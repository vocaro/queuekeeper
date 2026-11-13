# Lanes

What `qk run` and `qk admit` record of a run, how a lane's tests are counted, and two things a lane can
ask for: a compile step before its tests, and a report of tests no lane selects. A lane's keys are listed
in [lanes and admission](../doc/lanes_and_admission.md).

## Receipts and test counts

`python3 scripts/qk run LANE` writes `.build/queuekeeper/LANE/source.json` beside the lane's
`run.log`. It records the project's and toolkit's commit with a SHA-256 of their working changes
(tracked diff and untracked files), the policy commit for a heavy or exclusive lane, the argv
with any arguments given after `--` appended, those `arguments` on their own, and the exit
status or `failed-or-interrupted`. A lane's `compileFirst` step that fails records the outcome
`build-failed`, apart from `failed` and `unmeasured`, and no test ran (guides/app-testing.md,
Compile before the tests). After a zero exit, a lane with `minimumTests` fails unless
the log reports at least that many executed XCTest, Swift Testing, unittest or pytest tests; a run with
arguments needs at least one. `--expect N` requires exactly N. The count is the sum of each
XCTest bundle's top-level total (`All tests`, or `Selected tests` when filtered), each Swift
Testing run, each unittest run and each pytest run: `swift test` and `xcodebuild` run each bundle under its own
top suite, so the largest total counted one bundle (2,516 of a 3,355-test EPUBLib run, Xcode
27.1, October 2026). A wrapper's own `Executed N tests` line, one per simulator for example,
counts only when the log holds no XCTest totals that it would repeat, so a two-device run counts
both devices. A skipped test ran nothing and counts toward no floor. Each of these totals
includes its skips: XCTest names them in `with M tests skipped`, unittest in `skipped=M`, and
Swift Testing gives each test it skipped or that canceled itself a line of its own, which the
count subtracts (Swift 6.4, Xcode 27.1, October 2026). pytest's count is its summary line,
`1 failed, 10468 passed, 24 skipped in 589.31s`, whose passed, failed, skipped, xfailed and
xpassed tests ran; deselected tests, warnings and errors did not
([a pytest lane](parallel-tests.md#a-pytest-lane)). A run whose every selected test skipped
fails, saying how many skipped, and one that a test the host left unmeasured leaves short of its
floor is unmeasured. A wrapper that counts skips apart prints its own `Executed N tests, M
skipped`, with N leaving them out. Only the lane's own log is counted. A gate that writes each step's output to a log
of its own leaves no count there, so its lane either puts that output on its own output as well,
as PDFReflowLib's gate lanes do by running the steps serially, or sets no `minimumTests`.

A lane is unmeasured, exit 3, when its command exits 3, when its command exits 0 but its log
reports a test unmeasured ([honest outcome states](https://github.com/vocaro/vocaro-engineering/blob/main/guides/evidence.md#honest-outcome-states)), or when its
`timeout` stops it. Its receipt then records `"outcome": "unmeasured"`, with the log's lines for
those tests under `unmeasured`, or the timeout under `error`. A lane's timeout is a wall-clock
backstop that a busy host can reach
([core-wait time](https://github.com/vocaro/vocaro-engineering/blob/main/guides/measurement.md#cpu-scheduling-and-core-wait-time)), and the log it cuts short
counts nothing. A run that exits 3 is counted too, but a shortfall only prints, because the
unmeasured part may not have run at all. A run with arguments replaces the lane's last log and
receipt like any other run.

A run that ends records its verdict, the status `qk run` returns, as `outcome`: `passed` for 0
only, else `failed` or `unmeasured`. A zero exit that ran too few tests is `failed`, with the
shortfall under `error`. `eng release` counts this checkout's gate receipt only when it says
`passed` ([releasing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/releasing.md#tagging-and-publishing)). A failed lane whose log shows that no
test started, such as one whose project could not be generated, exits 4 rather than its command's
status and quotes the log's last error line in its summary and under `beforeTests`; it is still
`failed` (`lane_report.before_tests`, vocaro-engineering#393). A command's own status 2, a refused usage,
stays 2. A command that never started, as when the system cannot start it or `python3` is given a
file the checkout does not hold, exits 127 with `outcome` `not-started` and why under `notStarted`,
and the queue blames no commit for it ([#108](https://github.com/vocaro/queuekeeper/issues/108);
[maintenance](maintenance.md#the-merge-queue)).

`python3 scripts/qk admit -- COMMAND` records the same fields, with the directory the command
ran in and whether it was exclusive, in `.build/queuekeeper/admit/source.json`. Its output stays
on the terminal, so it writes no log, and the next admitted command in that checkout replaces
the receipt: copy it with the run's output when it is evidence.

## Every test has a lane

`qk doctor` reports this as `testLanes`, and warns unless every test has a lane. It finds XCTest and
XCUITest classes, Swift Testing suites and Python test modules in the tracked source, and reads what
each lane selects from its command: `swift test` and `qk xctest`, with `--filter` and `--skip`;
`qk unittest` and `python3 -m unittest discover`; and `xcodebuild test` or `qk simulator-tests` with
`-only-testing:Target/Class`.
`testLanes.result` is one of four:

- `every-test-has-a-lane`: every test found is selected by a lane the check could read.
- `tests-without-a-lane`: every lane was read, and `noLane` lists the tests none of them selects.
- `could-not-tell`, which is not a pass: a test no readable lane selects while some lane could not be
  read, as one that runs a project's own script or a scheme's test plan, since the test may be in
  it. `undetermined` counts those tests by directory and `unreadLanes` gives each lane's reason.
- `no-tests-found`: no tracked test of the kinds above, which says nothing of tests of other kinds,
  and is not a pass either.

In engineering.json, `testLanes.selects` states what a lane the check cannot read selects, as the
paths its tests are under, a directory ending in `/`, or `[]` for a lane that runs no test; a project
keeps that list true by hand, as the check takes it at its word. `testLanes.skip` maps a record's
path to the reason it is kept. `measurements/` and `evidence/` at the top of a project, and
`licenses`, `ThirdParty` and `Vendor` folders, are left out without naming them. A test counts when
any lane selects any of it, and the check does not ask whether a runbook says when that lane runs.
This is a warning, which changes no gate.

## Compile before the tests

A lane says so with `"compileFirst"`, an argv beside its `command`, which the toolkit cannot work
out from a project's own script:

```json
"mac-test-unsigned": {
  "command": ["scripts/app.sh", "mac-test-unsigned"],
  "compileFirst": ["xcodebuild", "build-for-testing", "-project", "app/App.xcodeproj", "-scheme", "App",
                   "-destination", "platform=macOS", "-derivedDataPath", ".build/native-tests",
                   "CODE_SIGNING_ALLOWED=NO"],
  "resource": "heavy", "timeout": 3600
}
```

`qk run` runs it inside the lane's one admission, on whichever Mac the lane goes to, before the
lane's command and within its `timeout`; arguments after `--` go to the command alone. Its
`xcodebuild` or `swift` has the stale-module rebuild the lane's own has. A build that fails ends the
run with the outcome `build-failed` in the lane's `source.json`, no test run and no count read, and
`run.log` holds the compiler's output; a build that reaches the `timeout` is unmeasured, as any lane
is. After a pass the compiler's output is in `compile.log` beside it, and `run.log` holds only the tests.

A self lane cannot have the step, as it has no admission of its own. A group runs its lanes'
commands without it: a gate builds what its suites run, and vocaro-engineering#269 covers what it leaves out. `qk xctest` and
`qk simulator-tests` compile first already, and stop on a failed build. For an `qk admit` run, or
a project with no lane for the run, chain the two yourself in one admitted job, `qk admit --
bash -c 'BUILD && TESTS'`, and pass the build the same derived data.

## The gate's time budget

A project sets a budget for `gate` in its lane, `budgetMinutes`, from the gate's measured time.
Only the gate lane has one.

The merge queue records each landed batch's gate run: its wall time as the queue saw it, wherever
the lane ran and with any wait for admission, and the parts its log reports with their times
(`qk unittest` and `qk xctest` processes, a lane group's lanes). Beside the wall time it records
the CPU busy share of the Mac the gate ran on, `cpuBusy`, the reading admission judges by
([#200](https://github.com/vocaro/vocaro-engineering/issues/200)). That is the mean of readings taken every minute while the gate runs,
kept with their highest, the limit that Mac sets (`busyLimit`, `HEAVY_BUSY_LIMIT`), the Mac and
the count of readings. The worker's log says them too.

When the median of the last 10 passing runs made on an unloaded host, with at least 3 to judge
by, exceeds the budget, `qk land` warns after its result and `qk doctor` warns, naming the
slowest parts ([#135](https://github.com/vocaro/vocaro-engineering/issues/135)). A run whose busy share was at or above its Mac's limit
is left out of that median, and the warning says how many it left out
([#235](https://github.com/vocaro/vocaro-engineering/issues/235)). A run with no reading, as before this record existed, counts.
`qk doctor` also warns of a direct-main `gate` with no budget, and of a `gate` that takes a
physical device: one that runs `qk device-pass`, `qk devices take` or `reserve`, or passes
`--device` to any other command. A `--device` of `qk simulator-tests` or `qk simulator-pool`
names a simulator type, which the pool supplies, so it gives no warning
([#113](https://github.com/vocaro/queuekeeper/issues/113)). A `--device` after such a command's `--`
belongs to what follows and still counts.

What a project does when its gate is over budget is its owner's policy; the toolkit's is in
[keeping the split](https://github.com/vocaro/vocaro-engineering/blob/main/guides/releasing.md#keeping-the-split).

## Scroll bars in a receipt

Queuekeeper records the kind of scroll bars each Mac run's host gives, read and never set: `scrollBars` in
a lane's receipt and in a dispatch's host identity, and one `queuekeeper: scroll bars:` line per Mac in the
lane's log. It reads the global `AppleShowScrollBars` (or `unset`) and any pointing device attached
other than a built-in one, by its HID usages; under the automatic setting any such device reads
as legacy. That holds for a mouse; an external Magic Trackpad or Magic Mouse counts by the same
rule, and what AppKit gives with one has not been measured. A reading that cannot be taken is
`unknown`, with why, and is never recorded as `unset`. It says what the host gives, so a process
that pins its own kind differs from it.
