# Parallel tests

How Queuekeeper runs tests side by side: lanes in a group, `qk unittest` processes, `qk simulator-tests`
simulators and `qk xctest` shards, with the limits that bound each run. What to run where, and how to
write tests that can share a host, is in the toolkit's
[parallel tests](https://github.com/vocaro/vocaro-engineering/blob/main/guides/parallel-tests.md) guide.

## Observe a quiet lane

A lane can opt into quiet-interval observations with `"stall": {"seconds": 600}`. The number
is the reporting threshold chosen for that lane, not a toolkit default. Begin in this report-only
mode and retain representative healthy slow runs before choosing an automatic stopping bound.
No production lane is enabled by this mechanism alone. Keep the ordinary `timeout` as a separate
wall-clock backstop. Put the setting on the admitted leaf that runs the work, never on a polling
wrapper or a lane group; each member observes its own log and processes, so another member's
progress cannot conceal a quiet lane.
The interval begins with the lane's primary command. A `compileFirst` preparation keeps its
ordinary timeout coverage; use a separately calibrated leaf lane to observe compilation too.

An explicitly declared pure primary controller may set `"primaryControl": true` inside
`stall`. The supervisor excludes only its exact primary PID/start identity from CPU progress;
all owned descendant work and the lane's output still count. This is for a controller such as
parallel pytest's master, whose periodic polling can otherwise conceal blocked workers. The
command must execute that controller as its primary process, with preparation outside the
observed leaf. Do not use this declaration for serial tests or a primary process that computes
the work itself. With no observable descendants the observation is unavailable, so the policy
cannot infer a stall from an empty worker set. It changes neither ownership nor cleanup and
does not qualify any stopping bound by itself.

The supervisor samples the lane's log metadata and cumulative CPU time of its owned processes
once per second. It verifies each PID's start identity, excludes recognized nested admission
owners, and treats process-set changes and changed log metadata as progress. An unreadable
CPU/log sample or a gap longer than three sampling intervals resets the quiet interval. Such a
gap can come from host sleep or scheduling delay; neither proves the work was idle. The sampling
cadence and gap tolerance are observation settings, not measured healthy quiet bounds.
An unavailable status writer also resets the interval and warns without stopping the command
or changing its outcome.
CPU below `ps`'s precision, legitimate I/O waits and commands that silently wait on a service remain
limits of the observation. Do not label an unknown wait as a pipe deadlock.

The current observation and maximum observed quiet interval are in `results/progress.json`,
with the host, run identity, observation time, live process identities, and unavailable-sample
count. A group retains each member's file in `results/MEMBER/progress.json`; these files travel
with the normal remote results. Threshold reports appear on the supervisor's standard error.
An ended observation is marked ended; an uncertain cleanup is marked uncertain. The file is
run evidence, not a live lease or permission to stop a process. Admission process records also
retain samples. `qk queue status` shows separate per-leaf progress observations, alongside
its admission wait, including the host, phase, quiet interval, threshold and sampling coverage.
Requests blocked on admission print the running holders' observations on a state transition
and at most every 30 seconds while that state continues, and `qk admissions now` lists them
under each holder on both Macs. The second Mac relays member
snapshots alongside its wait record; completed members disappear independently.

Each outer lane run creates a fresh namespace in its wait record; nested groups inherit it.
Readers select that namespace directly, so stale folders from older runs never consume the
bounded scan. Each leaf updates one stable slot keyed by its evidence path, retaining its
monitor run identity inside the record. Nonblocking publication locks keep an older run's
end marker from removing a newer run's slot. Remote snapshots use a stable host/leaf slot across dispatcher restarts and clear only
records still owned by their exact dispatcher PID/start in the current namespace. Old namespaces and retained evidence are left intact.
Live copies require the exact reporter PID/start, and admission copies the live guard identity.
Missing, ended, uncertain, oversized or stale copies are omitted;
a copy expires after 15 seconds without a fresh observation. That is display freshness, never
a stopping bound. Snapshot reads admit at most 128 records of at most 64 KiB each, with a
one-second process-identity census. Publication errors cannot change the command's verdict.
The retained file remains the evidence even after its live copy disappears. Retain it in the
owning project's private run records, without copying raw process arguments into shared documentation.

Calibration also records `observedSeconds` (intervals between consecutive observable samples)
and `maximumObservationGapSeconds` (the larger positive wall/monotonic gap), beside `samples`
and `unobservableSamples`. A gap above
three cadences and any unavailable sample break coverage; neither gap time nor the interval
leading out of an unavailable observation is counted. Review these coverage fields before
using a maximum quiet interval to choose a bound: sparse samples can hide a long quiet wait.

After reviewing a lane's healthy quiet measurements and hook exit-status forwarding, it may
declare `"stall": {"seconds": 600, "action": "stop", "reason": "Measurement and bound rationale"}`.
This is a configuration example, not a calibrated bound. Stopping rechecks output and CPU,
then uses the supervisor's existing TERM/KILL and nested-admission settlement. It raises the
typed `Stalled` outcome only after confirming the owned tree ended; the lane and group report
it as unmeasured (exit 3), and uncertain cleanup retains the admission guard. A hook that maps
every nonzero exit to 1 must preserve exit 3 before enabling this mode, so the merge queue
does not mistake incomplete output for a product test failure. Known polling controllers
outside admission ownership must stay outside the observed leaf; their own CPU can otherwise
hide a stall. No automatic stopping is enabled until that lane's bound is measured and reviewed.

The mechanism reuses the existing supervisor rather than adding a process library. GNU
[timeout](https://github.com/coreutils/coreutils/blob/master/src/timeout.c) supplies an elapsed
time limit, already present here; [pytest-timeout](https://github.com/pytest-dev/pytest-timeout)
is test-specific; [psutil](https://github.com/giampaolo/psutil) supplies counters and identities
but not this toolkit's admission and ownership rules. The evaluation and remaining calibration,
hook, and queue-display work are recorded on
[issue 340](https://github.com/vocaro/vocaro-engineering/issues/340).

## Suites side by side

A lane whose `parallel` names other lanes runs them at once under its own single admission:

```json
"lanes": {
  "mac-tests": {"command": ["swift", "test"], "resource": "heavy", "minimumTests": 3000},
  "ios-tests": {"command": ["python3", "scripts/qk", "simulator-tests", "--scheme", "Package-Tests",
                            "--runtime", "iOS-27-0", "--device", "iPhone-17-Pro",
                            "--device", "iPad-Air-13-inch-M4", "--"], "resource": "heavy", "minimumTests": 6000},
  "tests": {"parallel": ["mac-tests", "ios-tests"], "resource": "heavy"}
}
```

- `python3 scripts/qk run tests` takes one slot, starts every named lane, and writes each one's
  log to `.build/queuekeeper/tests/lanes/LANE.log` and, once it ends, its receipt beside it in
  `LANE.json`: its status, outcome, seconds and the executed and skipped tests its log counts, or
  the outcome `stopped` and no status when a stop ended it. Each lane is judged as `qk run LANE`
  would judge it: its `minimumTests`, its `timeout` and the unmeasured tests its log reports. A
  failed lane's last 40 log lines print under its result; the others still run to their end.
  The run ends with each lane's seconds, and the group's receipt records each lane's result.
- `jobs` bounds how many of the group's lanes run at once; it defaults to all of them.
- The group needs a `resource` at least as heavy as its heaviest lane. A lane that takes the
  screen cannot join a group, and a group cannot name another group. A group takes no
  arguments: filter one of its lanes instead (`qk run ios-tests -- -only-testing:T/C`).
- A group that runs a lane marked `"model": true` needs `"model": true` itself, or an exclusive
  resource. A group that runs two or more such lanes needs `"jobs": 1`: its one model lock
  cannot keep its own lanes from driving the model at once
  ([model admission](shared-host.md#model-admission)).
- A heavy or exclusive group goes to the second Mac whole, unless it or one of its lanes says why
  it stays on this Mac (`"here"`), and its lanes' logs and receipts come back with its own results,
  under `.build/queuekeeper/remote/GROUP/lanes/`
  ([which Mac runs a lane](shared-host.md#which-mac-runs-a-lane)). A gate script that runs such a
  group as one of its steps runs it with `qk run --here GROUP`, so the group runs where the gate
  is and leaves its results there; without `--here` a gate started outside an admission sends the
  group away and finds no results ([MathMLLayout](https://github.com/vocaro/MathMLLayout)
  `9520125`).
- `qk run --serial GROUP` runs the lanes one after another, on this Mac: the reference a
  failure seen only side by side is checked against ([parallel-only failures](https://github.com/vocaro/vocaro-engineering/blob/main/guides/parallel-tests.md#a-failure-seen-only-side-by-side)).
  `qk remote run --serial GROUP` runs them one after another on the second Mac, under its
  admission, for a group that runs there. The serial reference is no pass of the group's:
  `qk remote passed` counts only its lanes side by side
  ([how a run reaches the second Mac](shared-host.md#how-a-run-reaches-the-second-mac)).

A group is for lanes one admission can bound. A release checkpoint's lanes are not one: each is
heavy by itself, so `eng checkpoint` runs them one after another, each under its own admission
([checkpoints](https://github.com/vocaro/vocaro-engineering/blob/main/guides/releasing.md#checkpoints)).

Lanes that share a build directory cannot run side by side: two `swift test` runs in one
`.build` wait on its lock, and two xcodebuild runs need separate `-derivedDataPath`s. A gate
step that needs another's output stays a separate step before the group.

### A lane's results

`qk run` gives every lane's command the absolute path of a `results/` directory of its own in
`QUEUEKEEPER_RESULTS_DIR`, empty at the start of each run: `.build/queuekeeper/LANE/results/` for a lane
run alone, and `.build/queuekeeper/GROUP/results/LANE/` for a lane of a group, so that lanes side
by side never share one. What a command writes there, such as transcripts or a
[kept failure record](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#kept-failure-records), comes back from the second Mac into
`.build/queuekeeper/remote/LANE/results/` (or `.../GROUP/results/LANE/`) however the run ends,
and the dispatch names the group's lanes whose files came back. Read the variable rather than
naming the path in `engineering.json`: a fixed path is wrong inside a group, whose lanes' own
`.build/queuekeeper/LANE/results/` stays on the second Mac.

A lane run inside another, as a gate script's `qk run --here suites`, also copies its
`results/` into the outer lane's, as `results/LANE/`, however it ends, so that it comes back with
the outer lane's dispatch. A whole lane's `results/` is kept by commit beside its receipt, in
`.git/queuekeeper/remote/COMMIT/LANE.results/`, which the receipt's `keptResults` names,
for a run on either Mac; a later run of the lane at that commit replaces it, and one that wrote
more than 64 MiB keeps it only in its dispatch. `qk remote passed LANE` names the folder
(vocaro-engineering#218). A passing landing gate's log and `qk land --wait` name where its results
are and how many files they hold. A gate on the second Mac whose command writes nothing there, such
as a `qk unittest` lane, says that its command wrote none, since the second Mac sends back all it
writes there (#38). `qk disk` reclaims these folders by their rule, and never the receipts
([reclaiming disk space](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#reclaiming-disk-space)).

Per-test times come back that way. `qk simulator-tests` keeps, for each test run, a shard or a
device, `simulator-tests/KIND.log` and `KIND.json` in its lane's `results/`: the log's lines that
give each test's, suite's and run's outcome and seconds (Swift Testing's `✔ Test "t" passed after
0.7 seconds.` and `✔ Suite S passed after 4.0 seconds.`, XCTest's `Test Case '-[B.C t]' passed
(0.7 seconds).`, `Executed N tests` and failures), and the result bundle's summary
(`xcresulttool get test-results summary`, which lists each failure) with each class's or suite's
seconds (`suiteSeconds`), or `null` where the bundle could not be read. The logs folder's own
`DEVICE-SHARD.log` and `.xcresult` are overwritten by the next run in that checkout, on the second
Mac the next dispatch to the same slot. So EPUBLib's landing gate at COMMIT keeps its iOS shards'
times in `.git/queuekeeper/remote/COMMIT/gate.results/suites/ios-tests/simulator-tests/`,
under 1 MB for four shards (each shard's lines about 130 KB on 2026-11-06), and the Mac suite's per-test lines stay in the gate's `output.log`,
into which its script prints the group's lane logs. `KIND.json` also lists each test case's
outcome and seconds (`tests`).

A lane whose script runs `xcodebuild test` itself, because it prepares each device between runs,
keeps the same two files by calling, once per run, whether it passed or failed,

```bash
python3 scripts/qk results keep --log "$log" --bundle "$bundle" --name "$device" --status "$status"
```

which writes `simulator-tests/NAME.log` and `NAME.json` in `QUEUEKEEPER_RESULTS_DIR` (or `--results DIRECTORY`)
and prints one line per test case, with its outcome and seconds. A lane's output goes to its log,
so `qk run` prints those lines from `results/` when the lane ends, for a run on the second Mac once
its results have come back; it leaves out what `qk simulator-tests` kept. An outcome
is passed, failed, skipped, expected failure or unmeasured; an unmeasured test gives its reason, and
one with no recorded time prints none rather than zero. A bundle that is missing, unreadable or
holds no test case leaves `tests` empty or `null`, says why in `bundleUnread` and in the output, and
exits 3, the status Queuekeeper reads as unmeasured; a passing `xcodebuild` is then no evidence of which
tests ran. It exits 1 when nothing could be written, or when NAME is already in this run's results,
such as a run `qk simulator-tests` kept, so one record never replaces another: give each run a name of
its own. It copies no result bundle: a failure's goes in its
[failure record](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#kept-failure-records). What it writes follows the lane's
`results/`, emptied at each run's start and kept by commit under the rule above, so it needs no
pruning of its own (vocaro-engineering#260).

Pass `--activity-timing-prefix PREFIX` to retain XCTest activities titled `PREFIX N s`. For
example, `--activity-timing-prefix "Files Recents loaded after"` keeps the wait in
`Files Recents loaded after 5 s` as `{"title": "Files Recents loaded after 5 s", "seconds": 5}`
under that test's `activities` in `NAME.json`. Repeat the option to select more prefixes.
The result output prints the selected titles with their test names on either Mac. An empty
list means the test had no matching activity. A `null` means the activity tree could not be
read, and the result output says so. The test's outcome and duration still come from its
test record.

Keep the system trace when a test reports sustained loading, even if the test passes. Collect
the relevant services' logs from the start of the operation before releasing the simulator.
Put a losslessly compressed copy in `QUEUEKEEPER_RESULTS_DIR` and name it for the device and phase.
For an incident on the second Mac, cite the dated dispatch that `qk` prints. A later run can
replace the latest results and the copy kept by commit. Copy any trace needed beyond dispatch
retention to the project's evidence archive.

BreezEPUB's 480 s Files Recents wait kept its timing and passing verdict. The next gate reused
the checkout and replaced the system log before the follow-up investigation. BreezEPUB `30e1d59f`
keeps that capture in `simulator-logs/` alongside the test results. A fixture on the second Mac
verified that the returned gzip decoded to the original bytes after reuse of the validation path.
[Lost trace](https://github.com/vocaro/vocaro-engineering/issues/260#issuecomment-6764976504) ·
[Retention verification](https://github.com/vocaro/vocaro-engineering/issues/260#issuecomment-6766037464)

## Simulators side by side

`qk simulator-tests` builds a scheme's tests once and runs them on every named device at once:

```sh
python3 scripts/qk simulator-tests --scheme SCHEME --runtime iOS-27-0 \
  --device iPhone-17-Pro --device iPad-Air-13-inch-M4 --build-jobs 4 -- -only-testing:TARGET/CLASS
```

- Each device is a member of the project's
  [simulator pool](simulator-pool.md#reserved-simulator-pool), named in engineering.json's
  `simulatorPool` or by `--pool`: a free member of its kind is reserved in the device record and
  reset from its kind's warmed template (erased with `--erase`, or when the pool has no settled
  template to copy), or with `--keep-state` kept as earlier runs left it, or the next is cloned from
  that template, or
  created, and kept, after members eligible for pruning are deleted
  ([warmed templates](simulator-pool.md#warmed-templates),
  [keeping a member's state](simulator-pool.md#keeping-a-members-state)). It is
  addressed by its UDID, and shut down and released however the run ends, a stop included.
- The devices boot while `xcodebuild build-for-testing` builds for the first device. Building for
  one simulator builds only its architecture, where `generic/platform=iOS Simulator` builds
  every one. Each device then runs `xcodebuild test-without-building` on the same `.xctestrun`,
  with its own log, a new result bundle and a derived data folder of its own under
  `.build/simulator-tests/logs`, which it removes when the tests end: without one, every run
  leaves a folder of coverage data under `~/Library/Developer/Xcode/DerivedData`.
- `--build-only` builds and tests nothing. It takes the first device's member without erasing or
  booting it, only to name the build's destination, and returns it when the build ends. A gate
  can start it beside the stages before its suites, so that the full run with the same
  `--derived-data` finds the build up to date; that run waits for the build's hold on the folder
  (vocaro/EPUBLib#958).
- Arguments after `--` go to every device's test run, not to the build. A lane whose command runs
  `qk simulator-tests` ends with `--`, as in the example above, so that
  `qk run LANE -- -only-testing:T/C` appends its filter there; without it the filter is refused. `-C DIRECTORY` runs in a
  package's directory, where xcodebuild finds its scheme; `--xcode-project` or `--workspace`
  names a project instead. `--setting NAME=VALUE` adds a build setting.
- A boot still running at `--boot-timeout` (600 s) leaves that device unmeasured and the others
  run on; a first boot can stall while other simulators boot.
- Two simulators testing at once can hold the host's memory pressure at warning
  (`kern.memorystatus_vm_pressure_level` 2) for the whole run, where one at a time does not
  ([measured](https://github.com/vocaro/vocaro-engineering/blob/main/guides/parallel-tests.md#measuring-a-speedup)). While it is, admission takes no slot beyond the guaranteed
  ones, and a ceiling the pressure spoils is unmeasured
  ([unmeasured ceilings](https://github.com/vocaro/vocaro-engineering/blob/main/guides/measurement.md#host-pressure-and-unmeasured-ceilings)), so keep memory
  ceilings out of a run beside them. `--test-timeout` (2 h by default) is a backstop for
  a hung run, which it leaves unmeasured, and `--test-allowance` fails a single hung test
  ([bound every run](#bound-every-run)). A device that ran no test fails.
- The run prints `Executed N tests` for each device, adding `, M skipped` when any skipped, with N
  leaving them out, which a lane's `minimumTests` counts, and `, F failed` when any of the N failed,
  which the summary that ends `qk run` counts: the lines that name a failed test are in the device's
  log alone, so without it a failed Swift Testing test beside its bundle's empty XCTest run was
  counted passed (vocaro/queuekeeper#81). It counts them from the device's result
  bundle (`xcrun xcresulttool get test-results`), and from the console only when the bundle cannot
  be read. What a test process itself prints is in that bundle's action log
  (`xcrun xcresulttool get log --path DEVICE.xcresult --type action`), not in the device's
  xcodebuild log; and the next run with the same `--logs` folder makes the bundles anew, so read
  or copy them before it starts (a gate's bundles were lost that way on 2026-11-04,
  vocaro-engineering#172). A device whose every selected test skipped fails. It repeats every unmeasured line,
  each skip whose message begins `Test skipped - Unmeasured:`, as `DEVICE: -[TEST] : MESSAGE`, so
  the lane is unmeasured too.
- One run at a time uses a `--derived-data` folder and a `--logs` folder. A run that names one
  another run holds waits for it to end, before it takes a device, and prints `WAIT:` with the
  holder's PID. Simulators test from the build's products in place, and a build can replace a test
  bundle's embedded resource bundle whole: on EPUBLib, a build with no source change removed and
  copied it again, and for 8 of 8,377 polls in that build its `Fonts` folder was missing or held
  one of its two fonts, which a run testing from those products would have read
  ([vocaro/EPUBLib#872](https://github.com/vocaro/EPUBLib/issues/872)). A run that must not wait
  names folders of its own.
- `--jobs N` tests N devices at a time. `--jobs 1` tests them in turn: the serial reference.
- `--workers N` turns on xcodebuild's parallel testing on each device, which clones the device
  for each worker and deletes the clones itself. Its console then prints each test's outcome on a
  clone but no totals and no skip's reason (Xcode 27.2, 2026-10-31), which is why the bundle
  counts: on `tests/fixtures/XCTestShards` with 2 workers the console held no `Executed` line and
  no `Unmeasured:` reason, and the bundle held both, so the run counted 5 tests and 1 skip as it
  did one test at a time, exited 3 with the planted unmeasured skip and 1 with the planted
  failure. It stays off by default, as in the
  [simulator method](https://github.com/vocaro/apple-engineering/blob/main/guides/simulator-testing.md#xctest-execution-discipline), until a suite has been
  shown to pass with it.
- `--shards N` takes N members of each device's kind, and each runs a share of its tests in a test
  process of its own, one test at a time. The parallel workers do not share Swift Testing suites:
  xcodebuild ran every suite of EPUBLib's iOS bundle in one clone, all at once, where suites on the
  main actor took turns at each `await`, and 2,437 of 2,500 tests failed, timed out waiting for their
  readers or ended by a crash (Xcode 27.2, 2026-11-05). It lists the tests once with
  `xcodebuild test-without-building -enumerate-tests` and the run's own `-only-testing` and
  `-skip-testing`, packs them by class or suite into the shards, longest first by the seconds the
  last run's result bundles gave each (`shard-timings.json` in the logs folder), and gives each
  member its share by `-only-testing`, with parallel testing off; a class or suite the selection left
  only part of goes test by test. The device's line adds up its shards' counts. It and `--workers`
  exclude each other.

The Mac suite joins through a lane group beside the simulator lane, as in the example above.
`tests/fixtures/SimulatorSmoke` is the toolkit's positive control: one XCTest and one Swift
Testing test, which every device must count.

## Tests within one suite

| Runner | Side by side | One at a time |
| --- | --- | --- |
| Swift Testing | Tests run concurrently in one process by default | `swift test --no-parallel`, or `.serialized` on a suite |
| XCTest under `swift test` | `python3 scripts/qk xctest -j N`: N xctest processes ([below](#xctest-classes-side-by-side)) | `swift test`, the default |
| xcodebuild | `-parallel-testing-enabled YES -parallel-testing-worker-count N`: a clone per worker | `-parallel-testing-enabled NO` |
| pytest | pytest-xdist: `-n N --dist loadgroup`, with an `xdist_group` mark on tests that share state | `-n 0` |
| unittest | `python3 scripts/qk unittest -s tests -j N` | `python3 -m unittest discover -s tests` |

A wrapper that invokes the same selector on several devices or in several scenarios needs
[invocation identities in its aggregate results](https://github.com/vocaro/vocaro-engineering/blob/main/guides/evidence.md#repeated-test-invocations),
including when it runs those invocations one after another.

`qk unittest` discovers the suite as unittest does and runs each module in its own process, N
at a time (four by default). `-p PATTERN` limits it to the modules a file pattern matches, as
unittest's own does, and repeats: `-p 'test_core.py' -p 'test_remote.py'` runs both, and a test
two patterns match runs once. Until 2026-11-06 a second `-p` replaced the first without a word,
so a run meant for three modules ran one and gave its count as the whole. `--split class` or `--split test` runs each class or each test
apart instead; a module or class that sets `serial_group = "NAME"` runs in one process with every
other of that group, one after another. Each process runs in the directory Queuekeeper runs in and
imports from the top-level directory and then that one, as `python3 -m unittest discover` does. A
module whose `load_tests` loads another module from its file, under a name no process can import,
cannot be split: it runs whole in one process. Each process's output prints whole, under a line that
names it. The summary is not a `Ran N tests` line, so a lane counts each test once, and a
process that ran fewer tests than discovery found for it fails. The slowest module bounds the
run, so split a module that dominates: one that sets `shard_by = "class"` (or `"test"`) runs that
finely under any coarser `--split`, as the toolkit's tests/test_merge_queue.py does, whose 126
tests built real Git repositories one after another for 900 s while the rest of the suite
finished.

A run keeps its progress in a folder of its own under `.build/queuekeeper/runs/`, whatever its
standard output is, and names that folder in its first line, before discovery: `progress.log`
gets a line as each process ends, in whatever order they end, with the count of processes and
tests finished and failed, and `progress.json` names the processes running. When standard output
is a pipe, the first line comes on standard error as well. `qk progress` reads the folder from
another shell ([Long runs](../doc/agent_interface.md#long-runs), vocaro/queuekeeper#22).

With `--split class`, `--batch-size N` limits each class process to N tests in discovery order.
Modules that require a process per test, explicit serial groups, and modules that cannot be split
keep their own process boundaries.

To run part of a suite, name modules, classes or tests after the options, as `python3 scripts/qk
unittest -s tests test_merge_queue.Landing` does, or select them with `-k PATTERN`, repeated as
unittest's own `-k` is: an fnmatch pattern on the test's id, or a substring when it has no `*`. A
test runs when it is under a name and matches a pattern, under every `--split`, a module that runs
whole included. A name that matches no discovered test, or a selection that leaves none, fails
with status 5 and runs nothing (#219).

A run that does not finish says what it did not finish. A process past `--timeout` is stopped and
left unmeasured. A stop signal, such as the SIGTERM that supervision sends a lane's command at the
lane's `timeout`, stops every process: those not started never start, and those running end. The
run still prints its summary, with `N of M processes not run or not finished (stopped by SIGTERM)`
among what is unmeasured, and then ends by that signal. After the summary come the tests of every
process that did not finish, grouped by module, at most twenty modules, and the command that runs
only them, `python3 scripts/qk unittest -s DIR NAME...` with the run's `-p` and `-k`. Each NAME is
the module when all of its tests are unfinished, else a class when all of that class's are, else
the test. The whole list goes to `unittest/unreached.txt` in a lane's
[results](#a-lanes-results), with `unreached.json` beside it, or outside a lane to a folder of its
own under `.build/queuekeeper/unittest-unreached/`, which keeps the newest five. `qk run`'s summary
then gives the list's path and, for a lane whose command is `qk unittest` and that ran with no
arguments of its own, `qk run LANE -- NAME...`. The toolkit's gate timed out at 2,400 s on
2026-11-13 on a second Mac loaded by other work, with 75 classes unreached that nothing named,
and its log stopped mid-run as if it had crashed (vocaro/queuekeeper#37). Supervision now writes its
`supervise: timed out after Ns` line, and its line for a stall, into the lane's log as well as on stderr, which for a run on
the second Mac reached only the dispatch's `output.log`, and the summary of a dispatched run gives
the second Mac's reason as its `why`.

Keep tests that share state out of each other's way rather than turning parallelism off: a
group, an `xdist_group` or a serialized suite for the few that must share; per-test fixtures
for the rest ([tests own their files and defaults](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#tests-own-their-files-and-defaults)).

### A pytest lane

A pytest lane declares `minimumTests` as any other lane does, and runs pytest with
`-rs --no-fold-skipped`:

```json
"python-tests": {"command": [".venv/bin/python", "-m", "pytest", "-n", "4", "--dist", "loadgroup",
                             "-p", "no:cacheprovider", "-rs", "--no-fold-skipped"],
                 "resource": "heavy", "minimumTests": 10000}
```

- The count is pytest's summary line, its last, with or without its `=` frame and under
  pytest-xdist alike: `1 failed, 10468 passed, 24 skipped in 589.31s (0:09:49)` counts 10,469
  tests that ran, 24 of them skipped, so 10,445 toward the floor. Passed, failed, skipped, xfailed
  and xpassed tests ran; deselected tests, warnings and errors (a fixture's, or a module's that
  collected nothing) did not. `18 deselected in 0.01s` and `no tests ran in 0.01s` count 0. A
  plugin that adds its own item to the line, such as pytest-rerunfailures' `rerun`, leaves a line
  Queuekeeper does not read, so the lane fails its floor until Queuekeeper learns the word.
- A skip whose reason begins `Unmeasured:` leaves the lane unmeasured only if the log prints the
  reason, which pytest does in its short summary under `-rs` (or `-ra`, `-rA`). It folds the skips
  that share a file, line and reason into one line that names no test,
  `SKIPPED [2] tests/test_x.py:15: Unmeasured: REASON`, under `-rA` too; `--no-fold-skipped` gives
  each its own, `SKIPPED tests/test_x.py::test_y[1] - Skipped: Unmeasured: REASON`, which the
  receipt lists by test id. A folded line still makes the lane unmeasured, listing the line. `-v`
  is no substitute: it truncates the reason to the terminal's width, and under xdist prints none.
- Measured with pytest 8.4.2 and pytest-xdist 3.8.0 on Python 3.12, 2026-11-03, on a module of
  passes, skips, unittest skips, xfails, a failure, a fixture error and a warning, run with and
  without `-n 2`, `-q`, `-rA` and `--no-fold-skipped`; tests/test_core.py holds their lines.

### XCTest classes side by side

`swift test --parallel` reports a skipped XCTest test as passed, in its console and in its
`--xunit-output`, and prints no `Executed N tests` total (Swift 6.4.2, Xcode 27.2, macOS 27.2,
2026-10-31): 29 tests that skip without `EPUBLIB_EPUBCHECK_CLI` came back as 29 passes, where the
same filter run serially counted 29 skipped ([EPUBLib](https://github.com/vocaro/EPUBLib) `7e874a67`).
A skip that marks a result unmeasured would read as a pass, and a lane counts no test. Do not
use it.

`qk xctest` builds a package's tests once (`swift build --build-tests`, `--build-jobs` bounding
it) and lists them with `swift test list`. It packs the XCTest classes into three shards per
process, the longest class first into the shard with the least, by each class's seconds in the
last run of the configuration, kept in `.build/queuekeeper/xctest/timings-CONFIGURATION.json`; a
class with no time counts the median. Each shard runs `xcrun xctest -XCTest CLASSES BUNDLE` on its
module's bundle, one test at a time, and its output prints whole under a line that names it: every
skip, failure and `Selected tests` total, which a lane counts once each. Swift Testing's tests run
once beside them, in `swift test --skip-build --disable-xctest`, concurrently as it runs them.

Swift Testing runs a package's suites in that one process, at once: suites on the main actor take
turns at each `await`, so one test's window, defaults or fonts are there while another runs, and a
suite's `.serialized` keeps its own tests apart but not from other suites'. Under `qk xctest`
each XCTest class had a process of its own, one test at a time. `--split-swift-testing` keeps that
for Swift Testing: it packs the suites into the shards with the classes, by their seconds in the
last run as it does the classes, and runs each shard's suites in a
`swift test --skip-build --ignore-lock --disable-xctest --no-parallel` process of their own, one test
at a time, selected with `--filter` by suite (`^Module\.Suite/`) or by test. SwiftPM holds a package's
`.build` lock for the whole of a `swift test`, so without `--ignore-lock` each shard waited for the one
before it: 18 shards of EPUBLib's Mac suite took 1,522 s one after another, on 2026-11-05. A filter has no `$`: Swift Testing
matches it against an id that goes on past the one `swift test list` prints, so `^Module\.test\(\)$`
selects nothing (Swift 6.4.2, Xcode 27.2, 2026-11-05). Without it, two main-actor suites that each
waited 2 s took 2.1 s in one process, taking turns; with `--no-parallel`, 4.0 s.

Each Swift Testing process writes its event stream (`--event-stream-output-path`), and `qk xctest`
keeps every test's wall-clock start and finish from it beside its id, shard, verdict and seconds, in
the order the tests started, whatever order the shards' output prints in: in `xctest/` of the lane's
[results](#a-lanes-results), so a run on either Mac keeps them by commit, or in the package's
`.build/queuekeeper/xctest/` outside a lane. A test still running when its process timed out keeps its
start, unmeasured. `TEST_TIMES` and `test_times` in `src/python/queuekeeper/apple/xctest_shards.py`
say what each record holds (vocaro-engineering#367).

A project configures the run with options, not a script of its own:

- `-C PACKAGE`, repeated, tests several packages: each builds in turn, and their shards share
  the `-j` processes, longest first, each process named with its package.
- `-c release`, `--scratch-path PATH` (relative to each package) and `--swift-option OPTION`,
  repeated, go to every swift command of the run: its build, its listing, the bundle's path and
  the Swift Testing run.
- `--filter REGEX`, repeated, selects as `swift test --filter` does, by `Module.Class/test`.
- `--split-swift-testing` runs the Swift Testing suites in the shards, one test at a time, above.
- `--skip-build` runs the last build.

A process that fails, or runs other than the tests it was given, fails the run. A test that skips
with a reason beginning `Unmeasured:` ([honest outcome states](https://github.com/vocaro/vocaro-engineering/blob/main/guides/evidence.md#honest-outcome-states))
leaves an otherwise passing run unmeasured: it exits 3 and lists them last. A filter that selects
nothing runs nothing and exits 5. `tests/fixtures/XCTestShards` is its positive control: two
passing classes, a skip, a Swift Testing test and a Swift Testing suite of two, which
`--split-swift-testing` runs in shards of their own. `XCTEST_SHARDS_UNMEASURED` makes the skip
unmeasured and `XCTEST_SHARDS_FAIL` plants a failure, its negative controls.

EPUBLib's Mac suite at `7e874a67` on the second Mac (M1 Max, 10 cores, macOS 27.2, Xcode 27.2,
2026-10-31), heavy runs alternated with the load sampled rather than exclusive ones: its
`swift-tests` lane, one test at a time, held its slot 1,266 s, the one-minute load 19 when it was
admitted; `qk xctest -j 4 --build-jobs 4` took 495 s with a 54 s build at load 661, and 530 s
with a 6 s build at load 2. Each run counted 3,908 XCTest and 66 Swift Testing tests, 89 of them
skipped. Neither sharded run had a class's time to pack by: their 28 processes took 1,716 and
1,858 s together, and the longest 273 and 334 s, which bounded the run.

## Bound every run

A hang ends a run instead of holding it: on 2026-10-31 a BreezEPUB test waited from 08:13 on a
thumbnail request that never returned, holding a slot and a simulator, because nothing in the run
had a time limit. Two limits now apply to every run Queuekeeper starts.

- Each test has one, so that a hung test fails by itself, named, while the others run.
  `qk simulator-tests` passes `-test-timeouts-enabled YES` and
  `-default-test-execution-time-allowance` (`--test-allowance`, 600 s by default, XCTest's own
  default, which it rounds up to whole minutes) to every device's run; XCTest fails a test that
  runs past it, with a spindump, and goes on. A test that needs longer sets its own
  `executionTimeAllowance`; test arguments after `--` that set `-test-timeouts-enabled` replace
  Queuekeeper's. A script that runs `xcodebuild test` itself passes the same options, and a Swift Testing
  suite gives its tests a `.timeLimit`. Inside a test, every wait has a bound of its own
  ([waits bound stalls](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#waits-bound-stalls)).
- Each command has one, a backstop for a hang the first limit cannot see, such as a build or a
  runner that stops answering. A lane's `timeout`, `qk admit --time-limit`,
  `qk simulator-tests --test-timeout`, and `--timeout` for each process of `qk unittest` and
  `qk xctest` set it. A lane or `qk admit` command that sets none is stopped after 2 h
  (`processes.DEFAULT_LIMIT`), more than twice the longest admission the log held from
  2026-10-27 to 2026-10-31, 53 min of Pagepour's tests; a test process after 1,800 s, more than
  twice the toolkit's slowest, `test_remote`'s 817 s at load averages of 100 to 280
  (2026-10-31). At its limit Queuekeeper stops the command and every process in its group, which
  frees the simulator, and records the run unmeasured, never passed.

`qk doctor` lists the lanes that rely on the default. Give each a `timeout` of about twice its
longest measured run, as the gates' 14,400 s are, so that a hang ends soon after the run should
have.

A timeout stops a hang; it does not stop a gate from growing. The gate also has a budget,
`budgetMinutes`, from its measured time, which `qk land` and `qk doctor` warn past
([keeping the split](https://github.com/vocaro/vocaro-engineering/blob/main/guides/releasing.md#keeping-the-split)). The toolkit's is 20 minutes, the owner's
target for a gate with the host to itself (owner's decision, 2026-11-11). It was 15, about twice the
8.1 min median of six passing queue runs; set to 5 as a control, `qk doctor` warned and named the
slowest parts (2026-11-04, `e97577a`,
[vocaro-engineering#145](https://github.com/vocaro/vocaro-engineering/issues/145)). On 2026-11-11
the median of the last ten passing runs below the busy limit was 27 minutes.

The toolkit's `gate` is a group of three lanes, side by side, all three at once, `"wholeHost": true`
(owner's decision, 2026-11-09, [vocaro-engineering#289](https://github.com/vocaro/vocaro-engineering/issues/289)).
`gate-python` runs the Python suite in eight processes at a time, with up to four tests from one class per
process. `gate-swift-mac` runs the Mac suites of
`src/swift/EngineeringTestSupport`, and `gate-swift-ios` only the two suites whose drawing is UIKit's
on an iPhone 17 Pro and an iPad Air 13-inch (M4) simulator, with `--build-jobs 4`; the model suites,
GoldenTests, VoiceOverSpeechTests and SRGBPixelSamplerTests, run once, on the Mac. The width has no
measurement at the next higher bound. Each run was through `qk run` on the second Mac (M1 Max, macOS
27.2, Xcode 27.2, 2026-11-09):

| Lane | Run | Tests | Seconds | One-minute load after |
| --- | --- | --- | --- | --- |
| `gate-swift-mac`, alone | 22 s build | 47 passed, before SRGBPixelSamplerTests landed; 5 are negative controls recorded as known issues | 78 | 368 |
| `gate-swift-mac`, alone | 21 s build | 62 passed | 25 | 2 |
| `gate-swift-ios`, alone | first on that Mac: warms two templates and clones two members | 48 passed on each device | 1,169 | 632 |
| `gate-swift-ios`, alone | those members reused: 26 s build, 151 s in the simulator stage | 48 passed on each device | 208 | 973 |
| `gate`, the three at once | `gate-python` 1,523 s, `gate-swift-mac` 4 s, `gate-swift-ios` 97 s, builds warm | 2,284 passed, 1 skipped | 1,523 | 13 |

`timeout` is 300 s for the Mac lane, about four times its longest run, since a cold build was not
measured, and 2,400 s for the simulator lane, about twice its first run, which any Mac without warmed
templates repeats. The group's wall time in that run was `gate-python`'s. With four Python workers it took
25 minutes, over the then 15-minute budget and the 20-minute goal.

The worker limit comes from a fixed 139-test sample from 11 modules, weighted toward the merge
queue and remote dispatch tests that used most of the previous gate's Python worker time. On
`f7766859`, each trial ran the same test identities in separate processes, with zero skips and
failures. Four-worker runs bracketed each sweep under exclusive admission on the same host.

| Mac | Four workers | Eight workers | Twelve workers | Sixteen workers |
| --- | ---: | ---: | ---: | ---: |
| Workstation (18 cores) | 165.4 / 147.4 s | 122.2 s | 102.7 s | |
| Second Mac (10 cores) | 208.1 / 171.4 s | 116.5 s | 123.5 s | 130.1 s |

Eight workers was fastest on the smaller second Mac and improved the workstation's sample too.
The worker limit applies only to this gate's Python lane. [Timing records and full-gate verification](https://github.com/vocaro/vocaro-engineering/issues/145).

On the second Mac, a separate 120-test sample at eight workers took 120.7 and 121.2 s with one
test per process, 112.8 s in batches of four, and 131.0 s in batches of eight. The four-test batches
ran the same test IDs with zero skips or failures and reduced the process count from 120 to 48.
In an earlier comparison of the same 120 tests, whole-class grouping cut the sum of process times
by about 19%. It took 143.5–144.2 s elapsed, compared with 120.1–120.9 s with one test per process.
The longest class took longer than the entire per-test run. Compare elapsed time and the longest
shards on the same test IDs and worker count when choosing a batch size.
[Fixture batching measurements](https://github.com/vocaro/vocaro-engineering/issues/145#issuecomment-6774878492).

## Measured speedups

The toolkit's own Python suite, 159 tests in 11 modules, took 168 s with
`python3 -m unittest discover` and 67.5 s with `qk unittest` at four processes, on an 18-core
M5 Max (2026-10-24). The host's one-minute load average was 93 during the first run and 507
during the second, so neither time is a bound. The second was bounded by its slowest module,
`test_remote`, which took 63 s by itself and 19 s with `--split test`. At 179 tests, run back to
back at load averages near 200, the whole suite took 56.0 s by module and 43.6 s by test, so
the toolkit's lane splits by test.

A library's three suites, 3,100 to 3,450 tests each on the Mac, an iPhone 17 Pro and an iPad Air
13-inch (M4) simulator, run one test at a time within each suite, took 2,238–2,328 s one after
another in three runs and 816–830 s side by side in three, the simulators from one warm build, as
one lane group (M5 Max, 18 cores, 36 GB, macOS 27.0, Xcode 27.1, iOS 27.0, each run under
exclusive admission, 2026-10-24). The simulator lane alone took 822–838 s, so the Mac suite
beside it cost no wall-clock time. Beside the others each suite took about 5% longer. No failure
appeared only side by side: every Mac failure, serial or not, came while the display slept. Two simulators at once held memory pressure at warning in about 90%
of 15-second samples in five of the six runs that had them, and one simulator at a time never
raised it. Two cold builds at once were not measured.

`qk simulator-tests` ran `tests/fixtures/SimulatorSmoke` on an iPhone 17 Pro and an iPad Air
13-inch (M4) simulator side by side on an M1 Max build host, macOS 27.2, Xcode 27.1, iOS 27.0
(2026-10-24): one 26 s build, both devices tested at once, 2 tests counted on each, 162 s in
all, and both devices deleted afterward. Its two tests measure the cost of booting and launching,
not a suite's speedup.

On the pool, it ran the same fixture on the EPUBLib pool's iPhone 17 Pro and iPad Air 13-inch (M4)
on this Mac (M5 Max, macOS 27.0, Xcode 27.1, iOS 27.0, 2026-10-26, toolkit `cb1083a`). With no
member yet, a first run, whose build failed on a mistyped scheme name, created `EPUBLib iPhone 17
Pro 1` and `EPUBLib iPad Air 13-inch (M4) 1` and released both; the next passing run took and
erased both (2 tests counted on each, 43 s). With iPhone 1 held under another owner's
reservation, a third run created `EPUBLib iPhone 17 Pro 2` and took the iPad (2 tests on each,
125 s, the new member's first boot included). Every member ended shut down and released, and none
was deleted.

## Repeating a lane's tests

A test that failed only on a freshly erased member under load is not shown fixed by one passing run on
whatever member and load that run got. Run it as a series, which `qk run` admits and places as it does any lane:

    python3 scripts/qk run tests --repeat 10 --erase --only BreezEPUBTests/QuickLookTests

`--repeat N` runs the lane's `qk simulator-tests` tests N times on one build, every run on a member taken from
the pool afresh, by the take every run uses. `--erase` erases the member before each run; `--template` copies its
kind's warmed template, and a run the pool could only erase is unmeasured, so that a comparison of the two resets
compares only the reset it names. `--only TEST`, repeated for each test, replaces the lane's own selection, for a
lane that picks its own tests; a project's own wrapper script must pass its arguments on for that lane to take
one. The options reach `qk simulator-tests` as a lane's arguments, so a lane command that ignores them is
caught: `qk run` reads a series of the size it asked for from the log, and without one the lane is unmeasured.
A series holds the lane's one admission for all its runs, with N times the lane's `timeout`; each run's own test
limit stays `--test-timeout`. It is not a pass of the whole lane. A lane's `--shards` is set aside: each run is one
test process on each member.

Every run keeps its own outcome: a `SERIES DEVICE run I of N:` line with passed, failed, skipped or unmeasured, its
member and reset, its tests counted the same four ways, its seconds and its one-minute load at the start and end of
its tests, read on the Mac that ran it; a load the host gave no reading of says `unread`, not a number. The
`Repeat series:` line adds them up, and `series.json`, beside the logs and in the lane's `results/simulator-tests/`,
keeps each run and the totals. One failed run fails the series; otherwise any unmeasured run, or one that did not
happen, leaves it unmeasured. A failed build ends a series there, its later runs unmeasured. `--repeat 0` is refused
before anything runs, and a filter that selects no test fails each run.
