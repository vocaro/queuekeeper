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
`build-failed`, apart from `failed` and `unmeasured`, and no test ran (apple-engineering's guides/app-testing.md,
Compile before the tests). After a zero exit, a lane with `minimumTests` fails unless
the log reports at least that many executed XCTest, Swift Testing, unittest or pytest tests; a run with
arguments needs at least one. `--expect N` requires exactly N. The count is the sum of each
XCTest bundle's top-level total (`All tests`, or `Selected tests` when filtered), each Swift
Testing run, each unittest run and each pytest run: `swift test` and `xcodebuild` run each bundle under its own
top suite, so the largest total counted one bundle (2,516 of a 3,355-test EPUBLib run, Xcode
27.1, October 2026). A wrapper's own `Executed N tests` line, one per simulator for example,
counts only when the log holds no XCTest totals that it would repeat, so a two-device run counts
both devices. Within the output of a `qk unittest` process, under its `== NAME: OK (N tests, S s)`
line, only unittest's own total counts: a test there can print what the runner it fakes would, and
each `Executed 1 test` line of Queuekeeper's own tests once added a test to its gate's count
(vocaro/queuekeeper#153). A skipped test ran nothing and counts toward no floor. Each of these totals
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
those tests under `unmeasured`, or the timeout under `error`. A command that exits 3 itself, such as a
gate whose own timing check voided itself under load, says what it could not measure in a line of its log
that begins `Unmeasured:`, after any spaces or tabs, as `Unmeasured: CHECK voided its own measurement; rerun
on an idle Mac`. Such a line names no test and makes no run unmeasured by itself; when the log reports no
test unmeasured, the merge queue quotes the last such line in its worker log, its hold and `qk queue
status` ([maintenance](maintenance.md#the-merge-queue), [#30](https://github.com/vocaro/queuekeeper/issues/30)).
A line of any other prefix is not read. A lane's timeout is a wall-clock
backstop that a busy host can reach
([core-wait time](https://github.com/vocaro/vocaro-engineering/blob/main/guides/measurement.md#cpu-scheduling-and-core-wait-time)), and the log it cuts short
counts nothing. That holds when its cleanup cannot confirm that every process it used has ended:
the run keeps its admission reservation for recovery, but exits 3, not 2. A run that exits 3 is counted too, but a shortfall only prints, because the
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
[maintenance](maintenance.md#the-merge-queue)). A `qk run` whose Queuekeeper could not start, because
its plugins or one of the project's extensions did not load, exits 70 before it reaches the lane and
writes no receipt, and the queue blames no commit for that either
([#158](https://github.com/vocaro/queuekeeper/issues/158)).

A run that failed or ended unmeasured, and whose log counts no test and names none, ends its summary
with what its command last said, under `the last of its output:`: the log's last 5 lines that are not
blank and not Queuekeeper's own, each cut at 300 characters, or for an unmeasured run its `Unmeasured:`
lines when it has any (`lane_report.own_output`). A run that Queuekeeper explains itself in a `why:`
line, a lane group, and a run that stopped before its tests, which already quotes its last error, quote
nothing more. On 2026-11-13 BreezEPUB's `ways-in` lane, given two `-only-testing` arguments, exited 2
on the second Mac, and the summary said only `no test counts in its log` and `no failing test is named`
while the dispatch's `run.log` held the script's one line, `ways-in-native selects its own tests for
each run and takes no arguments`
([vocaro/queuekeeper#137](https://github.com/vocaro/queuekeeper/issues/137)).

A lane whose command takes no arguments says `"arguments": false`. `qk run LANE -- ARGUMENTS` then
exits 2 at once, as does `--repeat`, whose series reaches the command as arguments, naming the
arguments it was given, before the lane is staged, admitted or sent to the second Mac, and its summary
is `qk run LANE: not run (exit 2); no tests ran`. Left out or `true`, the arguments are appended to the
command as before. A lane group takes no arguments already, and does not take the key.

A run's receipt also keeps the load of the Mac that ran it, however the run ended, under `load`:
each reading of its one-minute load, its CPU's busy share and the busy limit that share is judged
by, taken every 60 seconds while the command runs and once at its end, with `null` for what could
not be read, and the highest and last load, the highest busy share and the last limit of those read
(`lane_report.RunLoad`). A run shorter than a busy reading needs has its busy share read over the
whole run. A lane sent to the second Mac is read there, by that Mac's own run, and its receipt comes
back within the result. The summary that ends `qk run` gives them in a `load:` line after the
tests, in the words a queue gate's send-back uses: `the one-minute load was highest 605.76, last
21.72 over 64 readings, and the CPU was at most 100% busy`. A run that did not pass and whose receipt
keeps no reading says so there. Before 2026-11-13 nothing recorded the load of a lane's run: a lane
that failed three times on the second Mac that day had it read by hand with `ssh build-host uptime`,
before and after each run, and after the first failure not at all
([vocaro/queuekeeper#133](https://github.com/vocaro/queuekeeper/issues/133)). Both figures count the
lane's own work, so neither says alone what else the Mac was doing.

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

## One selection on two idioms

With the Apple extension, `qk doctor` warns of a lane whose one command names an iPhone and an iPad
simulator type with `--device`, as `qk simulator-tests --device iPhone-17-Pro --device
iPad-Air-13-inch-M4` does, or a project's script given both. Such a lane runs one selection on both,
so each test whose result does not depend on the idiom runs twice
([#105](https://github.com/vocaro/queuekeeper/issues/105); apple-engineering's
guides/app-testing.md, Test dimensions and the snapshot layer). Three kinds of lane give no warning:

- a lane in `release.lanes`, or in a group named there, which runs every class on every simulator
  as the control for platform divergence;
- a lane whose `"idioms"` says in words why its whole selection depends on the idiom, such as
  where the project classifies its test classes, which the check takes at its word;
- a lane for each idiom, each naming one type, which is the project's own split.

The check reads only the lane's own words. A script that picks its simulators itself is not seen,
and neither is a `--device` of `qk device-pass`, which names a physical device. This is a warning,
which changes no gate.

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
the lane ran and with any wait for admission (`seconds`), and the parts its log reports with their times
(`qk unittest` and `qk xctest` processes, a lane group's lanes). Since 2026-11-13 it also records
that time in two parts: the gate's own work, from its admission to its end (`workSeconds`), and
the wait for admission before it (`waitSeconds`). The admission is when the lane's wait record
first says it was admitted, on this Mac or on the second Mac
([#112](https://github.com/vocaro/queuekeeper/issues/112)). Beside the times it records
the CPU busy share of the Mac the gate ran on, `cpuBusy`, the reading admission judges by
([#200](https://github.com/vocaro/vocaro-engineering/issues/200)). That is the mean of readings taken every minute from the gate's
admission to its end, kept with their highest, the limit that Mac sets (`busyLimit`, `HEAVY_BUSY_LIMIT`), the Mac and
the count of readings. A gate that ends before a minute of its own work has one reading, at its end.
The worker's log says them too. Each reading is of the Mac the lane's wait record names as it runs, which is the
second Mac from the moment the lane is sent there. Before 2026-11-13 it was the Mac the lane's dispatch record named,
and that record is filed only when the dispatch ends, so a gate that ran on the second Mac was read on this Mac and
judged by this Mac's limit: on 2026-11-13 three of Queuekeeper's gates on `build-host` were recorded against this
Mac's 70% and not that Mac's 90%, and one of them was left out of the median as loaded
([vocaro/queuekeeper#132](https://github.com/vocaro/queuekeeper/issues/132)).

The budget judges the gate's own work, not its wall time. On 2026-11-12 three of BreezEPUB's
whole-host gates did 4.7 to 6.7 minutes of their own work against a 15-minute budget, after
waiting 2.6 to 15.8 minutes for admission. The wait was most of their wall time, and no check
moved out of the gate shortens it ([#112](https://github.com/vocaro/queuekeeper/issues/112)). For the
same reason the busy share is read only after admission: a loaded run is one whose own work met a
busy Mac, not one that waited while the Mac was busy.

When the median own work of the last 10 passing runs made on an unloaded host, with at least 3 to judge
by, exceeds the budget, `qk land` warns after its result and `qk doctor` warns, naming the
slowest parts ([#135](https://github.com/vocaro/vocaro-engineering/issues/135)) and, beside it, the median wait for admission of those
runs and their median landing latency: the wall time as the queue saw it, wait included, which the
budget does not judge. A project sees which of the two is over: its own work, which moving a check
out of the gate shortens, or the wait, which it does not. A run whose busy share was at or above its Mac's limit
is left out of that median, and the warning says how many it left out
([#235](https://github.com/vocaro/vocaro-engineering/issues/235)). A run with no reading counts.
A whole-host gate's run is never left out for its busy share, and its record says `wholeHost`:
admission admits no other heavy work beside it, so the share is its own work. In 23 of the 25 runs
recorded by 2026-11-13, EPUBLib's and BreezEPUB's gates averaged at or above the limit, with nothing
else admitted ([#12](https://github.com/vocaro/queuekeeper/issues/12)).
A run recorded without `workSeconds` is left out of the median and not counted: one recorded before
2026-11-13, whose wall time holds a wait the budget no longer judges, or one whose admission was not
seen, as for a lane that ends within a second of it. Until a project has 3 passing runs on an
unloaded host recorded with `workSeconds`, nothing warns.
`qk doctor` also warns of a direct-main `gate` with no budget, and of a `gate` that takes a
physical device: one that runs `qk device-pass`, `qk devices take` or `reserve`, or passes
`--device` to any other command. A `--device` of `qk simulator-tests` or `qk simulator-pool`
names a simulator type, which the pool supplies, so it gives no warning
([#113](https://github.com/vocaro/queuekeeper/issues/113)). A `--device` after such a command's `--`
belongs to what follows and still counts.

The warning comes after every landing while the median stays over budget, so it does not ask each
session for a new proposal. Search the project's issue tracker for its open proposal on the gate's
budget and add to it; open one only if there is none. A reading the proposal already has need not
be repeated, unless it changes the picture, such as a much higher median or fewer runs left out for
load ([#96](https://github.com/vocaro/queuekeeper/issues/96), 2026-11-13). What a project does when its
gate is over budget is otherwise its owner's policy; the toolkit's is in
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
