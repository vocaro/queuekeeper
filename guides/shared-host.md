# Shared host

These mechanisms and lessons apply when the stated capability is present. Original
measurements establish their recorded platform and workload; they do not establish
universal behavior on later operating systems or in a different application.

## Heavy-work admission

On the shared host, builds, `swift test`, `xcodebuild test`, gate suites, corpus lanes and
conversions run through heavy-work admission: one host-wide resource namespace with bounded
concurrency. Run a configured lane with `python3 scripts/eng run LANE`; a lane whose `resource`
is `heavy` or `exclusive` is admitted, and a `light` lane runs directly. Arguments after `--`,
as in `eng run test -- --filter SUITE`, are appended to the lane's command. Run any other heavy
command with `eng admit`:

```sh
python3 scripts/eng admit -- \
  xcodebuild test -scheme SCHEME -destination "platform=iOS Simulator,id=UDID"
```

Add `--model` for a generative-model run: on-device generation on this Mac or in a simulator,
which uses the host Mac's model, and other local generative models such as MLX on the GPU. Core
ML whose compute units include the GPU takes it too ([model admission](#model-admission)). A
lane says so with `"model": true` beside
`"resource": "heavy"`. Add `--exclusive` only for work that needs the host to itself: a
whole-machine or wall-clock measurement, a model run that is timed or compared with a baseline
among them ([long-running measurements](#long-running-measurements)). Only the timed part of a
run is exclusive: build, install and set up first under `eng admit`, beside other sessions'
work, then admit the timed run alone with `--exclusive`, as a separate command, such as
`xcodebuild build-for-testing` and then `test-without-building`. Setup, recovery and a functional
run whose seconds are not meant as figures are heavy work. An exclusive request inside heavy
admission is refused, so a lane whose run needs both is `light`, and its script admits each part.
A gate suite is heavy work.
`--timeout` bounds the wait for admission. The command runs in the current directory with
its output on the terminal. Like a heavy lane, `eng admit` first refreshes the operating policy,
printing it to standard error, and records the command's receipt in
`.build/engineering/admit/source.json`. It needs no `PYTHONPATH` and works from a session
worktree ([locating the toolkit](adoption.md#locating-the-toolkit-from-a-worktree)). A toolkit
clone older than `admit` reaches the same queue through the resource CLI:

```sh
PYTHONPATH="$VOCARO_ENGINEERING_ROOT/src/python" python3 -m vocaro_engineering.resources -- COMMAND
```

Public project documentation may show plain commands for outside contributors on their own
machines; the admission rule is about this host. Project adapters (a `heavy_slot.py` shim,
scripts that import `vocaro_engineering.resources`) all call
`src/python/vocaro_engineering/resources.py`; there is no second queue to keep in step. A
private slot pool, such as `mkdir` locks in a scratch directory, is a second queue that the host
queue cannot see: it ignores load and memory pressure, and exclusive work cannot exclude it.
One such pool ran ten agents' builds beside another session's full gate, and the load average
reached 764 (October 2026).

Ten agents each building on every core, with other lanes and Xcode indexing, kept watchdogd from
checking in for 93 seconds and panicked an 18-core, 36 GB Mac (2026-10-14). A fixed limit of
three then left the host idle while queued work waited behind jobs blocked on simulators.
Overlapping gate suites also failed for no reason of their own: a test that bounded a nested
session with a fixed wall-clock budget expired when a second suite took the cores, and a red
gate that means "the host was busy" teaches people to rerun rather than read a failure. The
defect was the test's, not the host's. A gate's limits tolerate a busy host: a time limit leaves
out the seconds the work waited for a core
([core-wait time](measurement.md#cpu-scheduling-and-core-wait-time)), and a ceiling that memory
pressure spoiled is unmeasured, not failed
([unmeasured ceilings](measurement.md#host-pressure-and-unmeasured-ceilings)). Such a gate is
heavy work like any other; one whose tests still assume a quiet host stays exclusive until they
are fixed. A test that a busy host decided reports itself unmeasured, and its lane exits 3
([honest outcome states](evidence.md#honest-outcome-states)). Admission counts a gate as one job
however many workers it starts, so bound its build and test parallelism
([parallel tests](parallel-tests.md#bound-what-runs-at-once)).

`--jobs` does not bound every thread a build or test starts. A debug `swift test --jobs 2` is
bounded: its compiler frontends are build-system tasks, and samples every 2 s found no more than
two single-threaded frontends at once. A release `swift test -c release --jobs 2` is not, and
cannot be bounded today: every module compiles with whole-module optimization and `-num-threads`
set to the core count, and one frontend was sampled at 18 threads and 482% CPU. Swift Build
appends its own `-num-threads` after `-Xswiftc -num-threads -Xswiftc 2`, and the last value wins;
`swift build` has no way to pass a build setting through. A project whose gate builds in release
documents its compiles as unbounded rather than passing a flag that does nothing.
`xcodebuild test` takes a build task per core unless given `-jobs N`. A test that fans out with
`DispatchQueue.concurrentPerform` runs a thread per core for as long as it runs, so the test
bounds its own fan-out. These were measured on an 18-core M5 Max with Swift 6.4 and Xcode 27.1
(27A9275), SwiftPM building through Swift Build (2026-10-23). A build's `-num-threads` values
are in its Swift Build task store:

```sh
strings -n 1 .build/SCRATCH/out/Intermediates.noindex/XCBuildData/*.xcbuilddata/task-store.msgpack \
  | grep -A1 -- '^-num-threads$'
```

While no exclusive request waits, HEAVY_SLOTS jobs (3) are always admitted. Beyond them one job
at a time is admitted, up to HEAVY_SLOTS_MAX (half the CPU count), while the one-minute load
average is below HEAVY_LOAD_LIMIT (75% of the CPU count), macOS reports normal memory pressure
(`kern.memorystatus_vm_pressure_level` is 1), and no slot was taken in the last
HEAVY_SETTLE_SECONDS (30) for the load average to show. Setting HEAVY_SLOTS_MAX to HEAVY_SLOTS
restores a fixed limit. Admission cannot preempt: a job admitted while the host was quiet may
become heavy later. Exclusive work holds every slot up to the maximum. Each process reads these
variables from its own environment, so set them for every session or for none: an exclusive
job holds only the slots below its own maximum, and a caller with a higher one can take slots
above it.

Exclusive requests are admitted one at a time, in the order they asked. Each joins the queue,
`exclusive-queue.json` in the slot directory, at its first attempt, and is admitted once it is
first and every slot is free. While a live exclusive request waits, no other request is admitted
ahead of it: a heavy request waits even when a guaranteed slot is free, and prints that it waits
behind exclusive work. Jobs already running finish normally, so an exclusive request waits for
the work admitted before it asked and for the exclusive requests ahead of it, never for heavy
work that arrives later. Exclusive requests that follow one another hold heavy work back in turn.
A queued request holds nothing: one whose process has ended, or whose PID now has another start
time, is dropped from the queue, and a timeout or a stop takes the request out. A waiting process
keeps its place for as long as it runs, even orphaned from the script that started it; stop that
process to give up its place.

Nested work reuses its inherited admission. Admission exports `VOCARO_RESOURCE_TOKEN` and keeps
its lock descriptors open across exec. A nested `eng run`, resource CLI call or legacy gate
validates the inherited descriptors against their lock files, the owner's nonce, its guard
entry and its process start time, then runs inside the outer admission without taking a slot,
and without waiting behind a queued exclusive request, which waits for the outer job to end.
Nesting the queue therefore never obtains a second slot, and an exclusive request inside shared
admission is refused, as is a model request inside admission that does not hold the model. Work
that needs the whole machine asks for exclusive admission.

The toolkit's process supervisor passes a command only those of the token's descriptors that its
own process holds, each open on the lock file the token names, which names the owner. A process
started without them, as `eng unittest` starts each module's process and as a plain `subprocess`
call starts its child, keeps the token but not the admission. A nested admission there is
refused rather than given a slot, and the supervisor passes on none of the numbers the token
names: there each is closed, which would stop the command from starting with EBADF, or open on
another file, which the command would get.

The default macOS pool lives in the system's canonical user temporary directory, as reported by
`getconf DARWIN_USER_TEMP_DIR`, under the legacy `pdfreflow-heavy-slots` name, independently of
the command's TMPDIR. A private TMPDIR set before or inside the queued command joins the same
queue. HEAVY_SLOT_DIR is an explicit namespace override for isolated tests; a separate scratch
directory must not multiply the host's build budget.

A wait is expected. Admission prints `heavy: waiting (Ns): REASON` when it starts waiting and
every five minutes after. The reason names the limit that refused the request and the limit's
value, beside what the attempt read, so that a wait says which limit to look at:

| Kind | Reason, as printed |
| --- | --- |
| `slots` | `all 3 guaranteed slots are busy and so is every slot up to HEAVY_SLOTS_MAX=9`, or `… and HEAVY_SLOTS_MAX=3 allows none beyond them`; for exclusive work, `exclusive work needs every slot up to HEAVY_SLOTS_MAX=9 free, and 2 are busy` |
| `settle` | `all 3 guaranteed slots are busy and the load does not yet show the job admitted 12s ago, within HEAVY_SETTLE_SECONDS=30` |
| `load` | `all 3 guaranteed slots are busy and load 412.3 is at or above HEAVY_LOAD_LIMIT=13.5`, or `… the load average, which HEAVY_LOAD_LIMIT=13.5 bounds, is unavailable` |
| `memory` | `all 3 guaranteed slots are busy and memory pressure is level 2 (warning), not 1 (normal)`, or `… memory pressure, which must be level 1 (normal), cannot be read` |
| `model` | `the on-device model, one run at a time, is held by LABEL` |
| `exclusive` | `it waits behind exclusive work queued ahead of it: LABEL`, or `… holding the host: LABEL` |
| `legacy` | `a legacy gate, PID 4242, holds its lock /tmp/studywright-gate-suite.lock` |
| `guard` | `the guard entry of LABEL, PID 4242, is dead and requires inspection; …`, `uncertain` for an entry marked so, or `the guard PATH is foreign or unreadable and requires inspection; …`, where `…` names `python3 scripts/eng admissions recover` |

Each kind is the one the [admission log](#admission-log) records for the refusal. After its
timeout (5,400 s by default) admission fails without reclaiming any lock, naming the same reason,
and an exclusive request leaves the queue. A script that runs several jobs at once leaves at least one of the guaranteed
HEAVY_SLOTS for other sessions.

A dead or uncertain entry refuses every session's heavy work until it is recovered.
`python3 scripts/eng admissions recover` lists those entries, and
`eng admissions recover OWNER` removes one once its owner has ended and a census against its
process record finds nothing it ran still running ([lock reclamation](#lock-reclamation)).

A request's label names it in waiting messages, the guard and the [admission log](#admission-log).
A lane run's label is its project's name, from engineering.json, a space and the lane, such as
`EPUBLib gate`, so that the `gate` lanes of several projects, or the `tests` lanes of
BackgroundTaskLib and FileHandoffLib, stay apart. A request waiting behind EPUBLib's gate prints
`heavy: waiting (0s): it waits behind exclusive work queued ahead of it: EPUBLib gate`. An
`eng admit` request's label is its command line, and a caller that admits its own work names it,
as StudyWright's push keeper does with `warm push REV`. A toolkit older than October 27, 2026
labels a lane run with the lane alone, as in `gate`; its guard entries, queued requests and log
lines are read as they are.

Before a full gate, look for other sessions' runs across the host, not only under the usual
worktree directory: sessions also test from checkouts elsewhere, such as a share under
`/private/tmp`. The brackets keep `grep` from matching its own command line:

```sh
ps -Ao pid,args | grep -E '[x]ctest|[x]codebuild|[c]heck-all\.sh|[t]est-ios\.sh'
```

Several projects have a `check-all.sh`; its path says whose run it is. Never stop a process
merely because it is busy.

### Model admission

A generative-model run takes one heavy slot and the host's model lock, an `flock` on `model` in
the slot directory: both at once under the coordination lock, or neither. Two model runs exclude
each other while builds and tests take the other slots: one model workload per machine is a
limit of the hardware ([long-running measurements](#long-running-measurements)), and exclusive
admission of the whole host would hold every build and test back for the length of a run that
only the model needs to wait for. A model request waits behind a queued exclusive request, as a
heavy one does; while another run holds the model, it waits with the reason that the on-device
model is held, naming that run's label. Exclusive work holds the model with every slot, so no
model run starts beside it, even from a caller whose HEAVY_SLOTS_MAX is higher. The owner's
guard entry and the inherited token record `"model": true`. Nested work inside model or
exclusive admission reuses the model; a model request inside heavy admission that lacks it is
refused. The kernel releases the lock when the last process holding its descriptor ends, as it
does a slot.

A lane takes the model with `"model": true` and `"resource": "heavy"`; since exclusive admission
holds the model already, a lane is one or the other. A lane group runs its lanes under its own
admission, so a group that runs a model lane needs `"model": true` or an exclusive resource
itself, and a group that runs two or more needs `"jobs": 1`: its one model lock cannot keep its
own lanes from driving the model at once
([lane groups](parallel-tests.md#suites-side-by-side)).

Core ML whose compute units include the GPU (`.all`, `.cpuAndGPU`) takes the model lock too; only
Core ML pinned off the GPU (`.cpuAndNeuralEngine`, `.cpuOnly`) does not need it. A Core ML
embedder on all compute units shared the GPU path with on-device generation and returned
silently wrong vectors beside it; pinned off the GPU it returned correct ones
([embedding correctness](../knowledge/ml/embedding-correctness.md)). A model run that is timed or
compared with a baseline stays exclusive, because it measures the CPU as well. Not measured:
whether heavy work beside on-device generation slows it or changes its output. During
generation `TGOnDeviceInferenceProviderService` and `modelmanagerd` burn CPU, and builds beside
them compete for the same cores.

### Admission log

Each host logs its heavy-work admissions, so that an audit counts how often each kind of admission
is taken, how long requests wait and why, and how long each holds. An October 2026 audit of
exclusive work could not say, because nothing recorded past admissions. Admission appends one JSON
line when it admits a request and another when that admission ends, and a request that stops
waiting without admission appends one, as does a recovery that removes an entry. A refused attempt
writes nothing, nor does nested work that reuses its inherited admission. Each line is written
under the coordination lock that admission and release hold, so the lines are in the order slots
were taken and given back.

The log is `~/Library/Application Support/vocaro-engineering/admissions.jsonl`, in the account's
durable state, because an audit looks back weeks and files in the slot directory are the
temporary-file cleaner's after three idle days
([scratch and result directories](#scratch-and-result-directories)). A HEAVY_SLOT_DIR namespace
keeps its own log beside its slots. A line that would take the log past 8 MiB first makes it
`admissions.1.jsonl`, replacing the previous generation, so the two together stay within 16 MiB;
at two lines of 200 to 550 bytes, each holds 8,000 to 20,000 admissions. Each Mac keeps its own
log: the second Mac logs what is admitted there.

A line holds:

- `time`: when it was written, in UTC (ISO 8601).
- `event`: `admitted`, `ended`, `withdrawn` or `recovered`.
- `identity`: the request's nonce, which pairs an admission with its end and keys its guard entry;
  `pid`: the admitting process.
- `kind`: `heavy`, `model` or `exclusive`; `label`: the request's label, as waiting messages name
  it, up to 160 characters: `EPUBLib gate` for EPUBLib's `gate` lane, the command line for
  `eng admit`, or the label a caller gives, such as `warm push REV`.
- `slots`: the slot numbers held, on `admitted` and `ended` lines.
- `waited`: seconds from the request to its admission or withdrawal, on `admitted` and `withdrawn`
  lines; `held`: seconds from admission to its end, on `ended` lines.
- `holders` and `load`, on `admitted` and `withdrawn` lines: how many admissions the guard recorded
  when the request asked, a dead or uncertain one among them, and the one-minute load average then.
  A request admitted at once records nothing more.
- `wait`, on the `admitted` or `withdrawn` line of a request that an attempt refused: why it waited.
  `refusals` counts its refused attempts of each kind, about one a second; `first` and `last` are
  the kinds of its first and last refusals; `load` holds the one-minute load its last attempt read,
  as `last`, and the highest any read, as `max`; and `memory` is the highest memory pressure level
  read beside its refusals, 1 normal, 2 warning and 4 critical. A load or level that could not be
  read is null. A refusal's kind is the check that refused it:
  - `slots`: for heavy or model work, every slot up to HEAVY_SLOTS_MAX is busy, which is every
    guaranteed one when no adaptive slots are configured; for exclusive work, any slot is;
  - `settle`, `load` or `memory`: every guaranteed slot is busy, and a slot taken less than
    HEAVY_SETTLE_SECONDS ago, a load average at or above HEAVY_LOAD_LIMIT or one that cannot be
    read, or memory pressure that is not normal withholds another;
  - `model`: another run holds the on-device model;
  - `exclusive`: an exclusive request is queued ahead, or exclusive work holds the host, which
    refuses the request at its slots or the model;
  - `legacy`: a legacy gate holds its lock;
  - `guard`: a foreign, dead or uncertain guard owner requires inspection.
- `outcome`, on an `ended` line: `released` when the command finished, whatever its exit status;
  `not-started`; `stopped` or `timed-out` when a stop or the command's timeout saw it and every
  observed descendant end; or `uncertain` when the guard entry was kept for inspection
  ([lock reclamation](#lock-reclamation)). On a `withdrawn` line: `timed-out`, which includes
  `eng run --if-free` declining; `stopped`; or `failed`.
- On a `recovered` line, which `eng admissions recover` writes when it removes an entry: `pid` is
  the entry's owner; `uncertain`, whether the entry was marked so; `commands`, `processes` and
  `spaces`, how many unsettled commands, recorded processes and process groups or sessions its
  census checked.

An `admitted` line with no `ended` line is still held, or SIGKILL ended its admitting process,
which leaves its guard entry for recovery as well, and a `recovered` line follows once it is
recovered. A log that cannot be written never fails or holds up admission or release: admission
prints `heavy: the admission log was not written: ERROR` and goes on.

A request's `wait` says why it waited, which an audit could only infer from the holders logged
beside it. Whether admission's limits hold back work the host could run turns on the kinds: a wait
behind `slots` is the guaranteed and maximum counts, and one behind `load`, `memory` or `settle`
the signals that open the slots between, at the load its line records
([vocaro-engineering#49](https://github.com/vocaro/vocaro-engineering/issues/49)). `legacy` says
whether a legacy gate ever holds its lock, which retiring the shlock bridge waits on
([vocaro-engineering#25](https://github.com/vocaro/vocaro-engineering/issues/25)). A line an older
toolkit wrote has no `holders`, `load` or `wait`, and is read as it is.

`eng admissions` summarizes the log over a period:

```sh
python3 scripts/eng admissions --since 2026-10-20 --until 2026-10-27
```

It prints the times of the log's first and last lines, then, for each kind and each label within
it, the requests admitted and withdrawn, their mean and longest waits, what they waited behind,
the loads they saw, the mean, longest and total holds, and how the admissions ended: `recovered`
for one with no `ended` line whose entry was recovered, and `unlogged` for any other without one.
`waited behind load 9 (30m02s)` says that the load refused 9 requests at least once, and that
30m02s of their waits was behind it: each request's wait is divided among the kinds that refused
it in proportion to their refusals. Beside requests that say what they saw, those an older toolkit
logged count as `unrecorded`, with their whole waits; a kind or label with none of the former is
summarized as before. `load when asked mean 45.1, max 412.3, while waiting max 858.0` gives the
mean and highest load when its requests asked, and the highest that any attempt of a request that
waited read. A request counts in the period in which it was admitted or withdrawn. A date or time
without a zone is local. A period that begins before the log's first line is reported as such:
older requests have rotated out and count nowhere. Lines that cannot be read, such as one a full
volume cut short, are counted and skipped. Two projects' lanes of one name are counted apart, as
`EPUBLib gate` and `StaticSVG gate`; lines an older toolkit wrote count under the lane alone, as
`gate`. To summarize the second Mac's log, pass a copy, the previous generation first:

```sh
python3 scripts/eng admissions <(ssh build-host \
  'cd "Library/Application Support/vocaro-engineering" && cat admissions.1.jsonl admissions.jsonl 2>/dev/null')
```

### Load the queue cannot see

Admission reads the load before it admits and cannot account for work that never asks:

- Code-navigation (LSP) queries make `sourcekit-lsp` build the worktree at full parallelism.
  Agent sessions navigate with `grep` instead.
- Another session booting a fresh iOS Simulator ran the one-minute load average past 400 for
  two minutes on the 18-core host (October 2026).
- Xcode's indexing and an open Xcode's own builds.
- A long background measurement holds its checkout's SwiftPM build lock, so a `swift test` from
  a pre-push hook in that checkout waits silently behind it for up to the measurement's length.

## Second Mac

The owner's second Mac takes heavy work first. This Mac takes what has to run here, what the
second Mac cannot run, and heavy work while the second Mac is at capacity, until this Mac is too.
This Mac is the owner's workstation, busy with work the queue never sees, while the second Mac is
otherwise idle and has more memory and disk. eng, not the session, chooses the Mac; configuration
calls sending work there offloading it. The second Mac runs work under its own heavy-work
admission: each host's queue sees that host's load, so the second Mac is no private slot pool.
`eng remote` reaches it by the SSH alias `build-host`, or `second-mac`; `--host` or
`VOCARO_OFFLOAD_HOST` names another destination. Until 2026-10-28 the second Mac was the build
host. Records made until then, measurements among them, keep that name, and eng refuses the
configuration's earlier names ([earlier names](#earlier-names)).

```sh
python3 scripts/eng run LANE [-- ARGUMENTS]            # a heavy lane: the second Mac first, then this Mac
python3 scripts/eng run --here LANE                    # this Mac
python3 scripts/eng run --offload-only LANE            # the second Mac if it can start it now, never here
python3 scripts/eng admit --verify -- COMMAND          # a test or build: the second Mac, as a lane goes
python3 scripts/eng remote run LANE [-- ARGUMENTS]     # any lane, on the second Mac
python3 scripts/eng remote admit [--exclusive | --model] -- COMMAND
python3 scripts/eng remote passed LANE [--commit REV]
python3 scripts/eng remote setup [FORMULA ...] [--check]  # the gates' tools (Setting up the second Mac)
python3 scripts/eng remote setup --runtimes [--check]     # both Macs' simulator runtimes: their union
```

### Which Mac runs a lane

Every heavy or exclusive lane in engineering.json goes to the second Mac unless it says why it
stays on this Mac, as `"here": "REASON"`, which configuration validation requires to be a
nonempty reason. A lane says `here` when it needs a physical device, or a measurement tied to
this Mac's hardware, such as goldens rendered on this Mac's GPU. A foreground lane, which takes the
screen, runs only here anyway, and a light lane runs where it starts. Simulator lanes and
background Mac UI lanes go to the second Mac like the rest, and a lane's first run there is its
check (the owner's decision, 2026-10-28): counts that differ from this Mac's are a defect to fix,
not a reason to keep the lane here. A lane group runs its lanes under its one admission on one
Mac, so a group stays here when it, or one of its lanes, says `here`. `"buildHost"`, which once
marked a lane for the second Mac, is refused, true or false ([earlier names](#earlier-names)).

The second Mac's screen is usually locked, and `displaysleep` 0 there does not keep its display
on behind a lock made by hand. A command started over SSH there runs in an audit session of its
own and is not trusted for accessibility (on the 27.2 beta 26B5091g as on 27.2 26B123), yet the
window server gives it the console user's session: it opens offscreen windows and reads its own
accessibility. On macOS 27.2 (26B123), locked with the display awake, EPUBLib's Mac suite passed
there twice, case for case with this Mac's unlocked run, and its whole gate passed there behind
a lock made by hand, its Mac suite waking the display with the lane's `"display": true`
(2026-10-29). What a locked screen or a sleeping
display has been seen to stop, on which Mac and build, what wakes a locked screen's display and
keeps it on, and what is unmeasured, is in
[lock screen and display sleep](background-mac-testing.md#lock-screen-and-display-sleep).
A run whose mechanism a lock or a sleeping display stops is unmeasured, never passed
([shared resources](../agents/core.md#shared-resources)). Behind the lock, the accessibility API
(`AXUIElement`) vends no offscreen window on the second Mac's 27.2 either, as on this Mac on 27.0,
so a test that reads its own tree through it is unmeasured while the second Mac's screen is
locked; with its display asleep, a scroll that the display link animates gets no frame.
Pagepour's Mac lanes, `mac-tests` and `mac-purchases`, read their tree that way, and still go to
the second Mac: `here` would not help them, since this Mac locks too, and their tests move to the
in-process NSAccessibility methods instead
([Pagepour#123](https://github.com/vocaro/Pagepour/issues/123)).

`eng run` sends the lane to the second Mac, whole or filtered (`eng run LANE -- FILTER`), over
the working tree as it is: a tree with uncommitted changes or new files goes as a snapshot commit
([how a run reaches the second Mac](#how-a-run-reaches-the-second-mac)), so a session's iteration
over its edits runs there too. The second Mac comes first, and this Mac takes heavy work while
the second Mac is at capacity, until it is too (the owner's decision, 2026-10-28):

1. The second Mac runs the lane if it can start it now: its queue admits the lane within five
   seconds (`eng run --if-free` there, which exits 75 without running otherwise) and one of the
   project's four checkouts there is free.
2. When the second Mac declines it as busy, this Mac's own admission gets the same five seconds,
   and the lane runs here if it admits it. `eng: trying LANE on this Mac:` says why, and the
   receipt's `hereBecause` records that the second Mac was busy. Busy is no configuration problem.
3. While both are busy, the lane waits for whichever admits it first, the second Mac preferred.
   It waits in this Mac's queue, 15 s at first, doubling to 120 s, and between those waits it is
   offered to the second Mac again, so the second Mac is asked at least every two minutes; each
   offer costs the second Mac a checkout's claim, the project's `provision` lane and a policy
   refresh. An offer made again sends nothing a second time: the second Mac holds the commits
   after the first. `eng: LANE waits for whichever Mac admits it first:` says so, with what the
   second Mac declined it for, and this Mac's admission prints what it waits behind
   ([heavy-work admission](#heavy-work-admission)). A lane that neither admits within 5,400 s ends
   with exit 75 (`BUSY`) and runs nowhere.

Each of this Mac's waits that ends unadmitted leaves its queue and is logged as withdrawn, as each
declined offer is on the second Mac ([admission log](#admission-log)); an exclusive lane rejoins
this Mac's exclusive queue at each wait, behind requests that came meanwhile. `eng run --if-free`
asks each Mac once, five seconds each, and exits 75 when both are busy. The admission logs of
2026-10-27 hold this overflow before it was the rule: the build host's three `timed-out` requests
were `eng run --if-free` declining after 5.5 s, and each of those lanes then ran on this Mac
([vocaro-engineering#49](https://github.com/vocaro/vocaro-engineering/issues/49)).

First `eng run` checks, before it sends anything, that the second Mac can be reached: its name
resolves, an address of it answers on its ssh port within 3 s, and `ssh build-host true` finishes
within 30 s. The second Mac stays on its own wired network, while the owner takes this Mac to
others, where the second Mac's `.local` name does not resolve at all. The check then stops at the
name, after the 5 s that macOS waits for a multicast DNS answer that never comes (macOS 27.0.1,
2026-10-27), and sends nothing; an address that does not answer costs 3 s, not ssh's 10 s
`ConnectTimeout`. When this Mac is away from the second Mac's network in that way, when nothing
answers, or when ssh cannot connect, the second Mac cannot be reached, which is no configuration
problem: `eng run` prints one line, `eng: running LANE on this Mac:`, with why, and runs the lane
here, under this Mac's admission, at once (the owner's decision, 2026-10-27). It does the same
when the second Mac could not run the lane as it runs here: when the lane's arguments name a
physical device ([physical devices and the second Mac](#physical-devices-and-the-second-mac)), or
when the toolkit's checkout has changes to tracked files, since the toolkit goes only as a commit.

A second Mac that answers but is misconfigured stops the lane instead, for the owner's decision
([shared resources](../agents/core.md#shared-resources)): when it refuses the login, as for a key
it does not accept or a host key that changed, its login shell fails, it cannot receive the
commit or run the toolkit, it refuses the lane for any reason but being busy, as when its Xcode
at this Mac's developer directory differs or every checkout holds changes a run left, or it fails
the project's `provision` lane. `eng run` runs nothing here then. It says what is wrong and what
fixing it takes, and exits 78 (`EX_CONFIG`). A run that was sent finishes there: a connection
lost once the work was sent leaves the run unmeasured rather than running the lane a second time
here, since it may still run there.

`eng run --here` keeps a lane on this Mac, for instance a build whose products the session uses
here, or, once the owner has decided so, any lane while the second Mac is misconfigured.
`eng run --serial GROUP`, a group's serial reference, runs here too, and nested work runs inside
the admission it inherits, here. `eng run` looks for the second Mac only when
`VOCARO_OFFLOAD_HOST` names one or `~/.ssh/config` defines the alias `build-host` or
`second-mac`, which `ssh -G` reports; `VOCARO_OFFLOAD_HOST=none` keeps every lane here. A
lane's results come back to `.build/engineering/remote/LANE/` when it ran on the second Mac and
stay in `.build/engineering/LANE/` when it ran here; the last lines `eng run` prints name them.
A lane group's results hold each of its lanes' log and receipt as well, under `lanes/` as on the
Mac that ran it, so here `.build/engineering/remote/GROUP/lanes/LANE.log` and `LANE.json`. They
come back once the run there ends, whether the lane passed or failed, its timeout ended it or a
stop did. A lane's receipt gives its status, outcome, seconds and the executed and skipped tests
its log counts, so its counts read here; a lane a stop ended has the outcome `stopped` and no
status. A run whose connection is lost before it ends brings none back and is unmeasured; they
stay in the second Mac's checkout until a later run there replaces them.

Every run's receipt says where it ran. `ranOn` is `this Mac` or `second Mac`, which a receipt
written before 2026-10-28 gives as `build host`, and `mac` names the Mac. On this Mac,
`hereBecause` says why the run ran here: the lane's own reason, `--here`, `--serial`, nested
work, no second Mac configured, the second Mac being busy, or why it could not be reached or
could not run the work. The second Mac's own receipt, kept within the receipt that comes back,
names the Mac that sent the run in `dispatchedFrom`. A receipt holds its lane's last run only.
The share of heavy work each Mac runs over a period is in their admission logs, one line for
each admission: `eng admissions` here and on a copy of the second Mac's log counts each Mac's
admissions by lane ([admission log](#admission-log)).

The second Mac runs a lane in its own login shell's environment, not this Mac's, so a variable
set for `eng run` here does not reach it. On 2026-10-27 a variable set for `eng remote admit`
read as unset there (toolkit `e137d7e`). A run that a variable selects, such as one that points a
package at a binary built here into an ignored path, takes `--here`, or its lane names the
setting in its command.

`eng admit -- COMMAND` runs here. Its command runs in the current directory, often a
subdirectory of the checkout, and names paths relative to it, absolute paths in this checkout or
in a scratch directory, and often a simulator or device by its identifier, all of which belong to
this Mac. What it leaves, such as a build's products, DerivedData or a result bundle, is often
what the session uses next, here, and nothing about a command tells a test from such a build. Run
on the second Mac, in another checkout at another path, it would read and write the wrong places.
So the session says which it is: `eng admit --verify -- COMMAND` marks the command as
verification, a test or build whose result is its log and exit status and whose products this
Mac does not use. Such a command goes as a lane does, at HEAD or a snapshot commit: to the second
Mac first, to this Mac while the second Mac is busy or cannot be reached, and while both are busy
to whichever admits it first, within its `--timeout`; `--if-free` asks each once. It runs
in the directory of the second Mac's checkout that matches the current one, and its output and
receipt come back to `.build/engineering/remote/admit/`. eng keeps it here, saying why, when the
second Mac could not run it as it runs here: its directory is outside the checkout or one Git
ignores, which the commit sent lacks, or its words name this checkout's path, a simulator by its
identifier, which is a member of this Mac's pool, or a physical device of this Mac's record.
Nested in an admission, it runs inside that admission, here. A test that a lane covers runs best
as that lane, `eng run LANE -- FILTER`; `eng remote admit -- COMMAND` sends a command on purpose,
to run in the root of a checkout there.

A caller that holds or will take its own admission here, such as a push keeper that runs a gate
under the push's one admission, offers the lane with `eng run --offload-only LANE` instead:
`eng run` would run a lane it could not send under an admission of its own, and the caller would
then run it a second time. The second Mac takes it only if it can start it now, a tree with
uncommitted changes as a snapshot, so the status is that of the working tree the caller would
otherwise run; but it never runs here. The command exits 75 (`BUSY`) when the second Mac declines
the lane because none is configured, it cannot be reached or it is busy, or when the toolkit's
checkout has changes to tracked files, and the caller then runs the lane itself. It exits 78 when
the second Mac is misconfigured, which stops the caller too, and otherwise with the lane's status
there.

`eng doctor` compares the two Macs and reports a difference as an error: their default Xcodes
(`xcode-select -p` and its version), the Xcode at this Mac's developer directory, which every run
there uses, and the simulator runtimes that Xcode lists on each, as well as a second Mac that
answers but refuses the login. A second Mac that cannot be reached, as when this Mac is away from
its network, is no error, since work then runs here (the owner's decision, 2026-10-28): `unchecked`
says why it was not compared. A second Mac whose default Xcode differs still runs lanes, since a run selects this Mac's
developer directory, but whatever reads its default, such as `ssh build-host xcodebuild -version`
or a script without `DEVELOPER_DIR`, sees another Xcode. The two Macs keep the same runtimes:
when one has a runtime the other lacks, the error names the fix, `eng remote setup --runtimes`,
which brings both to the union of their runtimes and never deletes one
([setting up the second Mac](#setting-up-the-second-mac)). Under `offload` in the report,
`unchecked` names what was not compared: the second Mac, and why, when it cannot be reached, its
Xcodes and runtimes when it did not report them, and the whole comparison when no second Mac is
configured. `leftovers` lists the
processes on the second Mac that runs left running, which are no error
([processes left over from runs](#processes-left-over-from-runs)).

`connection` in the report says how ssh connects to the second Mac: the name and port `ssh -G`
gives, and the addresses that name resolves to under its `AddressFamily`, in the order ssh tries
them, each with the time a TCP connection to it took or why none was made, up to the first that
answers, after which ssh tries none. First addresses that do not answer, and so keep every
connection waiting before a later one does, are an error. When they are all of the other family,
it names the `AddressFamily` line that skips them
([setting up the second Mac](#setting-up-the-second-mac)). One refused at once costs nothing and is no
error. A name that does not resolve is in `unresolved`, beside the unchecked second Mac that
cannot be reached, and a second Mac that ssh reaches through a proxy is not measured.

### Simulators on the second Mac

A lane that boots simulators runs on the second Mac as it does here: headless, from a session
started over SSH, with the screen locked, and from the second Mac's own pool. It goes there like
any other heavy lane, and its first run there is its check (the owner's decision, 2026-10-28, in
place of the earlier rule that a simulator lane went there only once a run there had passed with
this Mac's test counts): counts that differ are a defect to fix there. One that also needs a
physical device stays here for that.

Each Mac keeps its own [pool](simulator-testing.md#reserved-simulator-pool) and its own
[device record](#device-reservations). A lane on the second Mac takes or creates members in the
second Mac's simulators and reserves them in the second Mac's record, so a member of the same
name on each Mac is two simulators, and a reservation on one Mac says nothing about the other's.
Physical devices, which both Macs reach, are reserved only in this Mac's record
([physical devices and the second Mac](#physical-devices-and-the-second-mac)).
A project's first run there creates its first members and lets their first boot finish. A
session's `VOCARO_DEVICE_OWNER` and agent session do not cross SSH, so a reservation there names
an owner the run generates: `simulator-tests-UUID` for `eng simulator-tests`, and `POOL-UUID` for
a gate that takes its member with `vocaro_engineering.simulators.pool.member()`.
`eng simulator-pool list` and `eng disk` show the pool of the Mac they run on; a run on the second
Mac prunes that Mac's members of the kind it takes.

Only logs and receipts come back: the lane's, or a group's and each of its lanes'.
`eng simulator-tests` leaves each device's log and result bundle in the second Mac's checkout,
where the lane's log names them, and puts a failing device's last 40 lines in the lane's log. A
session that needs a result bundle or a device log here runs the lane with `--here`.

Measured on 2026-10-27 with `eng remote run` from clean checkouts, against the same lane at the
same commit on this Mac
([vocaro-engineering#31](https://github.com/vocaro/vocaro-engineering/issues/31)). The build host
was an M1 Max (10 cores, 64 GB, macOS 27.2 26B123) and this Mac an M5 Max (18 cores, 36 GB,
macOS 27.0.1 26A434), both with Xcode 27.2 (27B48) and the same six iOS runtimes. Neither was
otherwise quiet, so no time is a bound:

- EPUBLib's `ios-tests`, an iPhone 17 Pro and an iPad Air 13-inch (M4) on iOS 27.0 side by side,
  passed with 3,667 tests executed and 43 skipped on each device, every test case with the
  outcome it had here, in 1,859 s against 1,428 s here.
- StaticSVG's gate passed with 501 tests executed and the opt-in probe skipped in each of its
  three suites, as here, in 192 s.
- MathMLLayout's gate passed its three Swift suites with 806 tests each, the iOS suite on a new
  pool member, as here, and left its validation tools and bundled corpus unmeasured: the build
  host had no Node, pnpm or `gate.py --setup` environments.
- EPUBLib's gate passed its Mac and iOS suites, the iOS one on the members `ios-tests` had
  created, and was unmeasured: the build host's corpus cache lacked 10 of the 50 pinned Project
  Gutenberg books, whose downloads no longer matched their pins, and it has no Standard Ebooks
  copy, so 3 more tests skipped in the Mac suite and on each device than here.

Creating two pool members and booting them for the first time beside a cold build ran the build
host's one-minute load average from 6 to 983 within four minutes, with memory pressure normal;
the 23 minutes of tests that followed averaged 40. No run needed the screen unlocked. The boots,
the builds and every gate, EPUBLib's Mac suite included, ran with the screen locked and the
display on; the screen was unlocked, not by the run, for the last 15 minutes of `ios-tests`.

### How a run reaches the second Mac

`eng remote` runs a commit: HEAD when the working tree is HEAD's, and otherwise a snapshot commit
of the working tree. It copies the checkout's index, with its modification time, to a temporary
file and, with `GIT_INDEX_FILE` naming the copy, takes the working tree with `git add -A`, tracked
changes, deletions and nonignored untracked files alike, writes it with `git write-tree`, and makes
the commit with `git commit-tree`, HEAD its parent. The session's own index, HEAD, branch and
working tree are untouched, and no branch or other ref holds the snapshot. Git reads a file whose
index entry is no older than the index, in whole seconds, and otherwise trusts its stats. A copy
dated when it was made therefore held what was staged of a file rewritten at its size within the
second it was staged: a test that does so failed that way in 4 of 72 runs, six at a time on this
Mac (2026-10-27, [vocaro-engineering#56](https://github.com/vocaro/vocaro-engineering/issues/56)).
Ignored files stay out, as they do from a commit: the second Mac's checkout has ignored inputs of
its own. A large untracked file that no `.gitignore` names is sent too, and its copy stays among
the repository's objects until Git's garbage collection prunes the snapshot, so ignore what the
second Mac does not need.
`eng run`, `eng admit --verify`, `eng remote run` and `eng remote admit` all send work this way.
The toolkit goes only as a commit: a toolkit checkout with changes to tracked files is refused.

`eng remote` takes no slot here. It pushes the commit, and the toolkit commit that runs it, by git
over SSH and without hooks to bare repositories under
`~/Development/build-host` on the second Mac, so nothing reaches GitHub. The connection that
prepares them first lists the processes earlier runs left running there
([processes left over from runs](#processes-left-over-from-runs)). There `offload_host.py`,
sent along and run by the host's own Python, takes one of four persistent checkouts of the
project, checks the commit out detached with the toolkit beside it, and runs `eng run` or
`eng admit` in the checkout's root, or `eng admit --verify`'s command in the directory that matches
the one it was sent from. The work it runs knows it runs there for this Mac, whose name
`VOCARO_DISPATCHED_FROM` gives, and its receipts say so. A checkout keeps its ignored inputs and
build products, so its next build is warm; `eng remote` waits for one when all four are in use.
A project's `provision` lane, when it has one, runs first to provision the checkout's ignored
inputs, and the work runs only if it passes. The work runs with `PYTHONDONTWRITEBYTECODE=1`, so
its Python leaves no `__pycache__` in the checkout, which a project's `.gitignore` need not name;
only a process that sets its own environment can still write one. A checkout that a run left with
changes stays as it is for inspection and is passed over. Once it is read, remove it with
`git --git-dir ~/Development/build-host/repositories/NAME.git worktree remove --force PATH`.

Output comes back as it is written. The lane's log, the command's output and a receipt land in
`.build/engineering/remote/LANE/` (`remote/admit/` for a command), and the exit status is the
second Mac's. A lane group's lanes' logs and receipts follow once the run there ends, a stopped
run's included, into `lanes/` there, and the receipt's `returned` lists those that came. Before
the run, the second Mac removes those its checkout holds from an earlier run, so none comes back
as this run's. The receipt names the second Mac's model, macOS and Xcode beside this Mac's. The
second Mac must have the same Xcode build at this Mac's developer directory, or the run is
refused. A different macOS is reported but refuses nothing: a whole lane's pass there counts
for a push from this Mac, while as a qualification claim it names the second Mac's platform. A
whole lane's receipt is also kept by commit in the repository's Git directory, so
`eng remote passed LANE` answers from any worktree whether the lane, without arguments, passed
at exactly that commit. A later run of the lane at that commit replaces it. A push path that
accepts it in place of running the gate again prints both Macs' macOS versions from the receipt.

The receipt names the commit that ran (`commit`) and the HEAD it was sent from (`head`), the same
commit unless `snapshot` is true. A snapshot's receipt stays in `.build/engineering/remote/LANE/`:
no branch holds the snapshot, so its result is no commit's pass, and `eng remote passed` never
counts it, for HEAD or for the snapshot itself. Commit the work and run the lane again for a pass
that counts. `eng remote run --serial GROUP` runs a group's lanes one after another there, under
the second Mac's admission: the group's serial reference
([parallel-only failures](parallel-tests.md#a-failure-seen-only-side-by-side)). Its receipt says
`serial` and, like a snapshot's, stays in `.build/engineering/remote/GROUP/`: it is no pass of the
group's, whose lanes run side by side.

A stop carries over. The second Mac's standard input is the dispatcher's lifeline: its end, from
a stop signal, the dispatcher's death or a dropped connection, stops the run there as a stop
signal stops a local one, and the run leaves the second Mac's admission. A run that returns no
result, such as one the network cut off, is unmeasured, never passed or failed. Foreground lanes
run only on this Mac, and the policy says what else stays here.

ssh closes a connection that has heard nothing from the second Mac for about two minutes
(`ServerAliveInterval` 30 s, four times), so a shorter silence leaves the run going. When a run
returns no result, `eng remote` says what ssh last said, such as `Timeout, server NAME not
responding.`, when the run was dispatched and when it was lost, in UTC, and how long this Mac
slept in between, read as the difference between a clock that runs through sleep and one that
stops for it. A Mac asleep runs no ssh. On 2026-10-26 a dispatch lost its connection around
17:05 UTC while its run waited in the build host's queue, and this Mac's power log
(`pmset -g log`) has it asleep with its lid closed from 16:59 to 17:04 UTC, apart from 45 s of
dark wake; nothing kept says how the connection ended
([vocaro-engineering#44](https://github.com/vocaro/vocaro-engineering/issues/44)).

A connection that ssh cannot make says what ssh said and what that shows. `ssh: connect to host
ADDRESS port 22: Operation timed out` means nothing answered the TCP connection within
`ConnectTimeout`'s 10 s. `Connection to ADDRESS port 22 timed out` means the connection was made,
so the second Mac's address answered, and the SSH exchange then timed out. After `Connection timed
out during banner exchange`, no SSH banner came within 10 s. Without that line, the banner came and
then nothing more for `ServerAliveInterval` times `ServerAliveCountMax`, two minutes, during key
exchange or authentication (OpenSSH 10.3p1 against a listener on this Mac, 2026-10-27). Each
connection also reads this Mac's sleep across the attempt, as a lost run does. A Mac asleep answers
nothing, so a timeout that this Mac slept through says nothing about the second Mac, and eng then
says to keep this Mac awake rather than to wake the second Mac.

On 2026-10-27 the build host accepted `eng run`'s first connection at 22:13:09 UTC, and this Mac
then slept until 22:16:39: its lid had closed at 22:11:35, and its power log gives
`Thermal Emergency Sleep` for each sleep after the first. The build host ended that connection, never
authenticated, at 22:15:12, about sshd's default `LoginGraceTime` of 120 s later. ssh's two-minute
timeout ran out at 22:18:38, two minutes of awake time after the connection began. ssh had said
`Connection to ADDRESS port 22 timed out`, and eng said to wake the build host, which had stayed
awake and on its network.

### Setting up the second Mac

The owner prepares the second Mac once:

1. Turn on Remote Login (System Settings, General, Sharing) and add this Mac's public key to the
   second Mac's `~/.ssh/authorized_keys`.
2. Name it in this Mac's `~/.ssh/config`, so that `ssh build-host true` succeeds without a
   prompt. eng takes the alias `second-mac` as well, in place of `build-host` or on the same
   `Host` line beside it, and tries `build-host` first ([earlier names](#earlier-names)):

   ```
   Host build-host
       HostName NAME.local
       AddressFamily inet
   ```

   `AddressFamily inet` keeps ssh to IPv4. The build host's `.local` name resolved first to an
   IPv6 link-local address that never answered, so each connection waited out `eng remote`'s
   `ConnectTimeout` of 10 s before trying IPv4: about 10 s for every connection, four for a
   project's run, where it now takes 0.3 s (2026-10-27). An ssh without `ConnectTimeout`, such as
   a plain `ssh build-host`, waited out macOS's own TCP connection timeout instead, 75 s
   (`net.inet.tcp.keepinit`). Neither Mac reached the other over IPv6 at all, while IPv4 worked;
   that is the network's, which is the owner's. `eng doctor` measures each address ssh tries, and
   reports first addresses that keep every connection waiting as an error
   ([which Mac runs a lane](#which-mac-runs-a-lane)).

3. Install the Xcode this Mac uses at the same path. Accept its license and run its first launch
   (`sudo xcodebuild -license accept`, then `xcodebuild -runFirstLaunch`).
4. Install Homebrew there (https://brew.sh); its installer asks for the second Mac's password.
   Then, from this Mac, install the tools the projects' gates use:

   ```sh
   python3 scripts/eng remote setup [FORMULA ...] [--import MODULE ...]
   ```

   It puts Homebrew on the login shell's PATH in `~/.zprofile`, since `eng remote` runs in the
   second Mac's login shell, and installs Homebrew's Python, `uv`, `shellcheck`, `xcodegen`,
   `zstd` and each formula a project names. A new login shell then checks every one, each
   `--import` module in that Python, and the Xcode at this Mac's developer directory, one line
   each, and the command fails while anything is missing. A second Mac without Homebrew stops it
   with nothing changed. `--check` only checks. A project's runbook names its own formulae.
   Without Homebrew, the system's Python 3.9 runs the toolkit, and lanes that need a newer
   Python fail.
5. Give both Macs the same simulator runtimes, and again whenever either gains one:

   ```sh
   python3 scripts/eng remote setup --runtimes [--check]
   ```

   It brings both Macs to the union of the runtimes that the Xcode at this Mac's developer
   directory lists on each, as `eng doctor` compares them, and it never deletes a runtime: one
   that only one Mac has is copied to the other. Both must have that Xcode at the same path. The
   copy is the runtime's own disk image, as `xcrun simctl runtime list -j` names it (for a
   cryptex, the one disk image of its listed size in its asset's `Restore` folder), so a beta
   build Apple no longer serves comes across exactly. It goes over the second Mac's SSH
   connection, never GitHub, into `~/Development/build-host/runtimes` on the second Mac or
   `~/Library/Application Support/vocaro-engineering/runtimes` on this Mac, outside
   `/private/tmp`, which a reboot empties. Only an image of exactly its listed size is added,
   with `xcrun simctl runtime add --move`, which leaves no second copy. A copy that fails or is
   stopped adds nothing and leaves nothing staged, and the next run removes what a reboot or a
   killed run left. Only a runtime whose image cannot be read is downloaded instead, by its
   build (`xcodebuild -downloadPlatform PLATFORM -buildVersion BUILD`). Before each copy or
   download it measures the free space where it lands and refuses one that would leave less
   than 40 GiB: room for simctl to copy the image once more where it cannot clone it, and for
   other sessions' building worktrees. It reports each runtime's size, copy and addition, then
   lists both Macs' runtimes again and exits 0 only when they match, otherwise 1 with what still
   differs. `--check` says what it would copy, download or refuse, changes nothing, and exits 1
   while the runtimes differ. One run at a time copies: a second refuses while one runs on this
   Mac. A copy of five runtimes moves about 40 GB, so run it in the background; it reads no
   input, and its last line is the outcome.
6. Keep it on power and awake. A sleeping second Mac leaves its run unmeasured. Its `.local` name
   is multicast DNS: it resolves only while the second Mac is awake on this Mac's network and
   the network carries multicast between them, and `dscacheutil -q host -a name NAME.local`
   shows whether it does. On the night of 2026-10-24 to 25 the name did not resolve from this
   Mac because this Mac was at another location, on another network, as the owner reported;
   neither Mac slept.
7. For the private toolkit's policy refresh and any private package dependencies, create a
   fine-grained GitHub token with read-only Contents permission on `vocaro/vocaro-engineering`
   and those dependencies (on all of the owner's repositories, if the owner prefers not to
   update it as dependencies change), and write it on the second Mac, readable only by its
   owner, as one line `https://USER:TOKEN@github.com` in
   `~/Development/build-host/git-credentials`. In zsh, without leaving the token in the shell's
   history:

   ```
   read -rs "token?Token: "; echo
   (umask 077; printf 'https://USER:%s@github.com\n' "$token" > ~/Development/build-host/git-credentials)
   unset token
   ```

The project's commits reach the second Mac over SSH without a GitHub credential. A heavy lane's
policy refresh fetches the private toolkit over HTTPS and needs read access to it through the
credential file above, even when the project has no private package dependencies. SwiftPM
clones a private dependency with git, and git's default helper on a Mac, osxkeychain, cannot read
the keychain from an SSH session: it fails with `-25308`, and the clone fails
([keychains on the right Mac](#keychains-on-the-right-mac)). When
`git-credentials` exists, `eng remote`
gives each run's git that file through the store helper and no other helper, by
`GIT_CONFIG_COUNT` variables that reach only the run, so the owner's own git configuration on
the second Mac is unchanged. A token reaches only the repositories it names, read-only, and it
expires: renew it before then. Check the second Mac with a light lane first, such as this
toolkit's `python3 scripts/eng remote run python-tests`.

Private GitHub release assets need the authenticated API as well as Git source access.
Declare their packages in `githubArtifacts`; admitted builds use the shared
[artifact preparation](public-access.md#private-binary-artifacts-and-build-credentials), whose
credential lookup reads this same run-scoped Git helper. Do not put a token into a package
manifest, asset URL or committed configuration.

### Keychains on the right Mac

A keychain is unlocked on one Mac, for the sessions that unlock reaches. Unlocking the
workstation's keychain does nothing for the second Mac, and an unlocked desktop says nothing about
a process that SSH started. An alias in this Mac's `~/.ssh/config` exists only on this Mac: given
in a shell already on the second Mac, `ssh build-host` said `Could not resolve hostname build-host`
(2026-10-28, [vocaro-engineering#65](https://github.com/vocaro/vocaro-engineering/issues/65)). So
before asking the owner to unlock a keychain, establish which Mac the work needs and which Mac
and session the owner's shell is in, and give only commands that check both. Never ask for the
password, and never put it in chat, a command's arguments, an environment variable, a log or
evidence: `security unlock-keychain` asks for it in the owner's own Terminal.

```sh
python3 scripts/eng keychain recover [--target MAC] [--keychain PATH] [--identity NAME]
python3 scripts/eng remote admit -- python3 scripts/eng keychain check [--keychain PATH] --identity NAME
```

`eng keychain recover` is the owner's, and unlocks nothing and reads no password. It:

- names this Mac, by the LocalHostName that receipts record, and the kind of session it runs in;
- finds the target, by default the second Mac, through the resolver `eng remote` uses
  (`VOCARO_OFFLOAD_HOST`, else the alias `~/.ssh/config` defines), and asks it for its name and
  the keychain file over the same bounded SSH connection `eng doctor` makes, which never asks for
  a password. On the second Mac, where neither names a destination, its device record, which
  every `eng remote` run makes defer physical devices to the sending Mac, marks it as the target,
  so the owner there gets the local command and no alias. `--target MAC` names this Mac or the
  second Mac by its LocalHostName;
- refuses, saying what to do instead, when the target is neither this Mac nor the second Mac,
  when the destination is `build-host` or `second-mac` and `ssh -G` gives it back unchanged, an
  alias this Mac's `~/.ssh/config` does not define, when this Mac names no second Mac and is not
  one, and when the second Mac cannot be reached;
- checks that the keychain file is on the target under its exact name. `--keychain` takes a path
  or a file name in `~/Library/Keychains`, which it never completes, so `login.keychain-d` is
  refused, with the keychain files that are there, closest first, to copy;
- prints the unlock commands. Each runs only on the Mac it names, as `scutil --get LocalHostName`
  reads it there, and on any other Mac says which Mac it is on and what to use instead, and fails.
  The command that goes through the alias, `ssh -t build-host …`, is printed only on the Mac whose
  `~/.ssh/config` defines the alias and runs only there, so pasted into a shell already on the
  second Mac it reaches for nothing. Its remote half checks that the alias reached the target, so
  an alias that names another Mac unlocks nothing there. On the target only the local command is
  printed;
- prints the check below last, for the context the automation runs in.

`eng keychain check` runs where the automation that uses the key runs: on the second Mac, through
`eng remote admit`, which runs it as every `eng remote` run is run, in its login shell under an
SSH login. It prints a JSON record with four separate results:

- `session`: the kind of session, `ssh`, `gui` or `background`, from the process's audit session
  flags, with a note on what a result there establishes. An SSH login is flagged remote and has no
  graphic access ([probing a session](background-mac-testing.md#probing-a-session)).
- `keychain`: `security show-keychain-info` on the file. `unlocked` when it read the settings,
  `locked` when Security refused it the interaction an unlock needs (errSecInteractionNotAllowed),
  `missing` for no such keychain.
- `identity`: whether `security find-identity -v -p codesigning` lists the identity `--identity`
  names in that keychain, by its SHA-1 or part of its name as codesign takes it, else any valid
  code-signing identity. It keeps counts, never names or hashes.
- `signing`: `codesign --force --sign NAME --keychain PATH --timestamp=none` of a temporary copy
  of `/usr/bin/true`, removed afterwards. It passes when codesign exits 0, which used the private
  key; it does not judge the signature's trust.

The outcome is `passed` only when signing passed, `failed` when signing failed, the keychain file
is missing or the identity is absent, and otherwise `unmeasured`, with each cause; the exit status
is 0, 1 or 3. A command that times out (30 s each, `--timeout`), cannot run, or prints what the
check cannot read leaves its result unmeasured, never passed, and so does a check without
`--identity`, which measures no use of the key. Each command gets no input. The check runs
`security` and `codesign` only in a session without graphic access, where Security cannot show a
dialog and refuses the interaction instead, as it refused osxkeychain over SSH above (-25308). In
a session with graphic access, or one whose flags cannot be read, it runs none of them and records
all three unmeasured: their manual pages do not say that they never raise a dialog, and there a
locked keychain or a key's access control can raise one.

- A result holds for the kind of session it was measured in. Signing that succeeds in the owner's
  GUI session, as an owner-local build does, does not qualify the key's use from SSH, nor an SSH
  result a GUI session. Whether an unlock reaches another session, such as an `ssh -t` unlock the
  next `eng remote` run, is what the check measures. Retry signing work once it passes there.
- Keychain access control and partition lists (`security set-key-partition-list`), trust
  settings, certificates, where credentials are kept, TCC and host configuration stay the owner's
  decisions. Neither command changes any of them; a failed check is reported for that decision.
- Not measured: either command against the second Mac, what `show-keychain-info` answers over
  SSH for a locked and an unlocked keychain on macOS 27, and the corrected workflow from a shell
  on each Mac ([vocaro-engineering#65](https://github.com/vocaro/vocaro-engineering/issues/65)).

### Earlier names

Until 2026-10-28 the second Mac was the build host, which read as the one Mac that builds,
though both do. Its configuration names became offload names, and since
[vocaro-engineering#58](https://github.com/vocaro/vocaro-engineering/issues/58) eng reads none of
the old ones. It refuses each, naming what replaces it, rather than ignoring it: a checkout or a
shell that still uses one meant something by it, and ignoring it would move that work silently.

- `"buildHost"` on a lane or group, true or false: configuration validation refuses it, naming
  every lane and group that gives it. Delete it, since every heavy or exclusive lane goes to the
  second Mac unless it says why it stays, with `here`; a checkout older than its project's change
  rebases onto main.
- `eng run --build-host-only`: refused, beside any other flag too; use `--offload-only`.
- `devices recover --build-host-unchecked REASON`: refused; use `--offload-unchecked REASON`.
- `VOCARO_BUILD_HOST`, set to anything, even beside `VOCARO_OFFLOAD_HOST`: refused; set
  `VOCARO_OFFLOAD_HOST` instead, to the same value. Ignored, it could send work to the second Mac
  that it was meant to keep here, so eng refuses it wherever it reads `VOCARO_OFFLOAD_HOST`: before
  `eng run` or `eng admit --verify` chooses a Mac, in `eng remote run`, `admit` or `setup`
  without `--host`, and in `eng doctor`. A command that neither chooses a Mac nor reaches the second Mac, such as a light
  lane, a lane that says `here`, `eng run --here` or `eng spelling`, never read it and runs as
  before, so a pre-push check, or the second Mac's own `eng run --here`, does not stop for it.

Nothing reads the other renamed words, so nothing refuses them: a receipt's `ranOn`, now
`second Mac`, and a device record's recovery, which records its check of the second Mac as
`offload`. Receipts and records written before keep `build host` and `buildHost`, since they are
data already on disk, and such a receipt still counts: `eng remote passed` and `eng release` read
the second Mac's receipts by commit, whatever their `ranOn`.

These keep the build host's name:

- The SSH alias `build-host`, which the owner defines in `~/.ssh/config`, stays the default, since
  the toolkit cannot change that file. eng also takes `second-mac`, as `Host second-mac` in place
  of `Host build-host` or both on one line, and without `--host` or `VOCARO_OFFLOAD_HOST` uses the
  first of the two that `ssh -G` resolves.
- `~/Development/build-host` on the second Mac, which holds its bare repositories, its warm
  checkouts, the runtime staging and `git-credentials`: another name would make every checkout
  cold and leave the token behind.
- `refs/vocaro/build-host/`, under which a push carries a commit into those repositories: another
  prefix would orphan the refs already there, such as one a lost run left.
- Records made before the rename, measurements, logs and receipts among them, and the guides'
  dated observations, which keep their words.

## Scratch and result directories

A worktree that builds a package and its apps holds 8 to 14 GB of SwiftPM and Xcode output, and
a full data volume fails every session's shell with `ENOSPC`. Before fanning sessions out into
build worktrees, check `df -h /System/Volumes/Data` and budget about 12 GB for each; when the
budget does not allow it, work one at a time in one worktree. Put a scratch build
(`--scratch-path`) outside the repository and delete it once its result is read. Remove a
worktree once its work has landed.

A change to documents or scripts that builds nothing needs no full checkout. Add the worktree
with `git worktree add --no-checkout`, list the paths with `git sparse-checkout set --no-cone`,
then run `git checkout`. A link check needs each changed document's local link targets on disk,
and a hook that lints every document on disk needs every document and its targets. That set was
13 MB of studywright's 2.5 GB tracked tree (October 2026). Check `extensions.worktreeConfig`
first: with it on, the sparse setting stayed in the new worktree's own configuration and left
the main checkout whole.

Keep a run's results directory only while it is worth reading: a run that passed removes its
own, and a run with a failure or an unmeasured result keeps it and prints the path. Prune
earlier runs' directories by count, naming each one removed. Keeping every run's results once
filled a 1.8 TiB volume in three days. Concurrent runs share TMPDIR, so record the owning
process's PID and start time in a directory before it takes the name the pruner matches. The
pruner leaves a directory whose recorded process still has that PID and start time (a reused PID
has a different start time and protects nothing) and counts only finished runs against the
limit. A directory made by an older tool that records no owner is pruned as finished, so the
protection holds only between runs that record owners.

Key a scratch package that is kept between runs by checkout, for example
`NAME/<directory name>-<checksum of the checkout path>`. One scratch shared by every checkout
raced: two builds started together failed on a manifest holding one checkout's text and the
tail of the other's, and runs started 20 seconds apart passed while one ran a harness compiled
from the other checkout's source. Two runs from one checkout still share its scratch.

The macOS temporary-file cleaner deletes files in the user's temporary directory that have not
been read, written or changed for three days, and leaves the directories. A kept SwiftPM scratch
then holds dependency checkouts without their `Package.swift`; SwiftPM takes them as resolved
and fails with "the package manifest … cannot be accessed" (observed on macOS 27, October 2026).
When a build fails in a scratch an earlier run left, discard that scratch's build directory
once, say so, and build again from nothing. Report a failure only after the clean build.

A reboot empties `/private/tmp`. The reboot after the 2026-10-14 panic removed a coordinator's
scratch, a shared baseline, every agent's notes and the handoff records kept there; the
worktrees survived. The restarts that installed the 2026-10-26 macOS updates also emptied both
Macs' user temporary directories (`$TMPDIR`), and a failed gate's lane logs went with the build
host's. Keep notes, counting scripts and proposed commit messages that must outlive
a reboot in the worktree's ignored `.build/`. The device record now lives outside `/private/tmp`
([device reservations](#device-reservations)).

Durable inputs live outside `/private/tmp` too. Keep pinned model weights and other verified
downloads, which are too large for a worktree and shared by every checkout, in
`~/Development/models/PROJECT/`, one folder for each pinned snapshot, and make a project's
download script and backend documentation default there. That folder survives reboots and the
temporary-file cleaner, has the same path on the second Mac, and is never an `eng disk`
candidate. `~/Library/Caches` is no substitute: macOS treats caches as regenerable and may delete
them when the volume runs short, which on this host happens when sessions are busiest.
`eng doctor` warns when the project's tracked Markdown names model storage under `/tmp` or
`/private/tmp`: a path there that names models or weights, or that a variable whose name holds
`MODEL` is set to. It skips files under a `measurements` or `evidence` folder, whose records keep
the paths their runs used.

Claude Code keeps each session's scratchpad under `/private/tmp`
([reclaiming disk space](#reclaiming-disk-space)), so a session whose work may span a reboot
copies its deliverables (scripts, probe sources, results) into the project's evidence folder as
it makes them. Before reasoning about what survived, read `last reboot`, not only `uptime`, and
compare the reboot times with when the files were last used.

## Reclaiming disk space

The rules above slow growth; they do not recover what crashed or finished sessions left behind.
Reclaiming that needs proof that nobody's live work is in it, and a quiet session, a passed
deadline or a finished-sounding name proves nothing. `eng disk` takes the inventory and changes
nothing:

```sh
python3 scripts/eng disk
```

It covers the whole host, whichever project runs it, and measures only the items it lists. It
prints each item's size, the reason for its status and the exact command that would remove it.
A **candidate** is proven unowned by the rules below. An **owner** item shows no sign of a
session using it, but only the owner can say whether it is still wanted: Xcode's default
simulators, a runtime no device uses, archives, test results, a directory no repository has
registered and a worktree outside the session directories. Everything else is **kept**: in use,
owned, of unclear ownership, or holding work or evidence. `--map` also reads the whole home
directory, `/private/tmp` and `/Applications` with `du`, which takes several minutes, to list
the largest other directories, never candidates, and to say how much of the used space it could
not measure.

- **In use.** One `lsof` and `ps` census of the account's processes is taken first. A path is in
  use when a process's working directory, an open file or a path in its arguments lies inside
  it, and a path in use is kept. Every candidate has also been unchanged for `--min-idle-hours`
  (24 by default): a worktree created a moment ago at origin/main is clean and landed before its
  session's first process starts.
- **Linked worktrees** of every main checkout in `~/Development`, from `git worktree list`. A
  candidate lies in its project's `.claude/worktrees` or `.codex/worktrees` or in
  `~/.codex/worktrees`, is unlocked (Claude Code locks an agent's worktree while the agent
  runs), has no process, is clean (no modified or untracked path) and has landed: its HEAD is
  contained in origin/main, or every one of its commits has an equal patch there and none is a
  merge. Ignored files under a `measurements` or `evidence` directory keep it. The inventory reads
  origin/main once per repository and does not fetch, so work that landed after the last fetch
  stays kept. Its `git status` runs with `GIT_OPTIONAL_LOCKS=0`, so it never refreshes another
  session's index. The command is `git worktree remove` without `--force`, which itself refuses
  a dirty tree and leaves the branch. A session left open in a removed worktree cannot run
  commands there again; its work is on main and its branch remains.
- **Simulators**, from `simctl list -j`. A member of a pool that a main checkout's
  engineering.json names in `simulatorPool` is a candidate only under the
  [pruning rule](simulator-testing.md#pruning), and its command is `eng simulator-pool prune`,
  which reserves it, checks it again and records the deletion. A device left by the retired
  [disposable lifecycle](simulator-testing.md#disposable-simulators), whose unique name carries a
  UUID, is a candidate when it is shut down, unused for the idle time, absent from every
  process's arguments and files, and unnamed, by UDID or name, in every device record: the one
  the devices module reads, the account's record when `VOCARO_DEVICE_RECORD` names another, and
  any legacy `/private/tmp` handoff file. Every device is shut down and unused by any process
  before it can be a candidate, and an unreadable record keeps every device. Xcode's default
  devices and unavailable named devices are owner items; any other named device was made by
  hand and is kept. A runtime no device uses is an owner item: a pool may create its members on
  it next.
- **Build caches.** A DerivedData folder in `~/Library/Developer/Xcode` (named
  `NAME-<28 letters>`) is a candidate when its `info.plist` names a workspace that no longer
  exists; one without `info.plist` is an owner item, and the folders every build shares
  (`ModuleCache.noindex`, `SDKExplicitPrecompiledModules`) are kept. A SwiftPM scratch (a
  directory holding `workspace-state.json`) or an Xcode `-derivedDataPath` directory under
  `/private/tmp` or the user temporary directory is a candidate when it lies outside every
  checkout. Claude Code keeps each session's scratchpad in
  `/private/tmp/claude-UID/<checkout path, each other character a hyphen>/<session>/`; while
  that checkout exists, or a process still works inside its path (a session whose worktree was
  removed can still be running there), the session may return, so its scratch builds are kept.
  Hyphens in names make that match loose, which only keeps more. A clone or artifact in SwiftPM's
  cache, `~/Library/Caches/org.swift.swiftpm`, is a candidate too: SwiftPM fetches it again.
- **Results.** Result bundles, and run directories whose `owner` file records a finished run's
  PID and start time, are owner items; a run whose recorded process still has that start time is
  kept.

`eng disk --delete` runs only on a terminal and asks once per category. Immediately before each
removal it takes a fresh census and checks that candidate again, refuses an `rm` inside any
working tree or reached through a link, and appends every decision to
`~/Library/Application Support/vocaro-engineering/disk-reclaimed.jsonl`. It never runs an owner
item's command. Sizes are allocated sizes from `du`; APFS clones share blocks, so removing one
can free less than its size. Run the inventory again after removing worktrees: DerivedData whose
workspace went with them becomes a candidate.

On 2026-10-21 the 1.8 TiB data volume had 21 GiB free. Of about 1.74 TiB used, a measurement
archive held 314 GiB, linked worktrees 150 GiB (nearly all in use or unlanded), `/private/tmp`
116 GiB (76 GiB of it agent session scratch, mostly Xcode derived data built in scratchpads),
simulators 79 GiB (one default iPhone 23 GiB), simulator runtimes 38 GiB, and applications,
another account and a virtual machine about 300 GiB together. About 600 GiB lay in folders the
shell cannot read, which macOS privacy protection hides (`~/.Trash`, Mail, Messages, app
containers), and in system data; `--map` reports that remainder rather than guessing at
it. The inventory found 37 GiB of candidates, almost all derived data in finished sessions'
scratchpads, and 76 GiB of owner items, 48 GiB of them Xcode's default simulators.

A free-space reading taken after a write failed with `ENOSPC` says nothing about the moment it
failed. From 2026-10-24 to 2026-10-26 the build host's data volume filled completely ten times,
for 3 to 11 minutes each, while something used its free space at 25 to 90 MB/s. Each time about
1.7 TB came back at once, and readings a few minutes later found about 1.6 TB free. CacheDelete's
purge requests marked every fill and nothing else in two weeks of the unified log, which kept
them long after most other messages, the kernel's included, were gone (about nine hours). To
date a fill, search them in the host's local time:

```sh
log show --start '2026-10-26 11:00:00' --end '2026-10-26 12:00:00' \
    --predicate 'process == "appstoreagent" AND eventMessage CONTAINS "Purge request with desired bytes"'
```

While the volume is falling, symptomsd's log names each process that has dirtied 2 GB
(`log show --last 1h --predicate 'eventMessage CONTAINS "local storage writes trigger"'`), and
`lsof +L1` lists files deleted while still open, whose space `du` cannot see.

### Processes left over from runs

Those fills began the evening `eng remote` work started on the build host, came every six to
seven hours, and stopped at the restart that installed macOS 27.2 (26B123), at 02:05 UTC on
2026-10-27. The restart also emptied `$TMPDIR`, and with it what could have named the writer.
Nothing in the toolkit was found to write the space, and the investigation closed it as a likely
defect of the macOS 27.2 or Xcode 27.2 beta that the restart replaced
([vocaro-engineering#45](https://github.com/vocaro/vocaro-engineering/issues/45)). The other
explanation it weighed was a process that a run left running until the restart, and supervision
cannot see a child that moved to a session of its own and was orphaned between two samples
([process supervision](#process-supervision)). So the second Mac is searched for such processes
before work is sent there: `eng remote`, and `eng run` for a lane it sends there, print what the
search finds in the connection that prepares the second Mac's repositories, before anything is
pushed, and `eng doctor` lists it under `offload`, `leftovers`. A process is listed when it is
the account's, runs from a checkout there (`~/Development/build-host/checkouts`), the
temporary directory (`$TMPDIR`) or a simulator's device directory, and no live run accounts for
it:

- A process runs from a place when its working directory, as `lsof` reports it, lies in it, or
  its command line names a path in it. A program under `/System` is macOS's own:
  IMTransferAgent works in the temporary directory on both Macs.
- A live run accounts for the process when the process or an ancestor is a live admission
  owner, as the guard or the [admission log](#admission-log) records it, or a live
  `offload_host.py`, which writes its PID and start into the checkout slot's lock file when it
  takes the slot: a light lane, and a project's `provision` lane, take no admission. A recorded
  PID counts only while its process has the recorded start, and an admission that the log has
  not ended counts only for a process that started before it.
- Some of a run's processes launchd starts or adopts, and what a live run holds is its own with
  everything below it. A simulator's processes descend from `launchd_sim`, never from a run, so
  a device is a live run's while the second Mac's [device record](#device-reservations) holds
  it, unreleased, for one of that run's processes with its recorded start, or while one of them
  names its UDID. The reservation is the exact signal: `eng simulator-tests` names its devices
  by their kind, so the run names a device only while an `xcodebuild` of its names it. A pool
  member's reservation records the process that took it (`holderProcess`), and an active run's
  records its process.
- An Xcode program, such as the `DTServiceHub` that `xcodebuild` starts and leaves to launchd
  once it ends, is a live run's when it runs from a checkout slot in which a live admission's
  owner works, and started no earlier than that admission, by the log's time or else the owner's
  start. That window is the only signal it leaves, so other programs from the checkout are
  still listed.
- Once the admission ends, or the reservation's process is gone or another process has its
  PID, they are listed. On 2026-10-27 a dispatch listed a live EPUBLib `ios-tests` run's three
  `launchd_sim` processes, each with about 230 descendants, and two `DTServiceHub` processes from
  its checkout. The run ended at 22:43 UTC; by 23:27 all were gone. The search cannot tell a
  device the owner booted by hand from one a run left booted, and lists both.

Each leftover whose parent is not itself a leftover is listed with its PID, its parent (1 once
launchd has adopted it), its start, its command line, where it runs from, and the bytes it has
written to files since it started, with each of its descendants and what that one has written.
The bytes are `proc_pid_rusage`'s logical writes, which read the account's own processes without
sudo or `fs_usage`; `top -stats` has no per-process writes. They count a write when it is made,
flushed or not, and one to a file deleted since, but none to a pipe or `/dev/null`. The same
call's disk I/O count left out a buffered 4 MiB that the process never flushed itself (macOS
27.0, October 2026). The search took 0.3 s for 1,174 processes on the build host (2026-10-27).

The search only reports. A search that fails says why, and the work is sent all the same. Tell
the owner what it lists, with its PIDs and their writes: whether to stop a process is the
owner's decision, as is any other change to a shared host
([shared resources](../agents/core.md#shared-resources)).

## Device reservations

Physical devices, and the members of a project's simulator pool, are
used only under a real reservation in the host's one device record, a human-readable JSON file
outside every repository: `~/Library/Application Support/vocaro-engineering/device-handoff.json`
of the account the sessions run as. `src/python/vocaro_engineering/devices.py` is its only
writer. It finds the record from the account's home directory, not `HOME` or `TMPDIR`, so a
session with a private environment joins the same record; `VOCARO_DEVICE_RECORD` names an
isolated record for tests only. Committed documentation keeps no device identifiers or live
session details. An entry records:

- the device and the owning session, with the revision the helper assigns;
- reserved, testing or released state, whether a run is active, and the process whose end ends
  the run; for a simulator pool member, the process that took it;
- the purpose, the expected release time and the bounded run being attempted;
- the original accessibility settings, and the apps, outputs and owner data to preserve;
- at handoff, the terminal outcome, the restoration checks and the explicit release time.

Read and change the record through the helper's CLI, or from a script through
`devices.Record()` and its `reserve`, `update`, `release` and `recover` methods:

```sh
export PYTHONPATH="$VOCARO_ENGINEERING_ROOT/src/python"
python3 -m vocaro_engineering.devices status
python3 -m vocaro_engineering.devices show iPhone    # the entry and its revision; 0 if new
python3 -m vocaro_engineering.devices reserve iPhone --owner SESSION --revision N \
  --device UDID --purpose "…" --release-by 2026-10-21T18:00:00Z \
  --approved-by OWNER --approved-at 2026-10-21T15:02:00Z --approved-until 2026-10-21T18:00:00Z \
  --set 'originalSettings={"voiceoverEnabled": false}' --set 'preservation=…'
python3 -m vocaro_engineering.devices update iPhone --owner SESSION --revision N --set 'run=…'
python3 -m vocaro_engineering.devices release iPhone --owner SESSION --revision N \
  --outcome "…" --restoration "…"
```

The record is a coordination record, not a mutex: agree ownership with the other session first.
Each change takes an exclusive lock, reads the record fresh, refuses unless the entry is still at
the revision the caller read and, for an update or release, still the caller's, and replaces the
file whole without rewriting any other entry. A refusal changes nothing: read the entry again
and decide afresh. Each change prints the entry's new revision, which the caller's next change
names. The lock covers one change, never a reservation. One device is never held under two
keys, and the helper takes only a canonical uppercase UDID, never a name or `booted`.
`update KEY … --own PID` records a process the session launched, by its PID and start time;
`release` and `recover` refuse until each has exited. The record also holds `mac-foreground`,
the owner's screen, as [attended Mac sessions](#attended-mac-sessions) describe.

Sessions used to keep ad hoc records in `/private/tmp`. In October 2026 two were live at once,
so two sessions could reserve one device in different files; unlocked read-modify-write could
lose another session's entry; and the restart after the 2026-10-14 panic emptied `/private/tmp`
while device work could still be running. The helper refuses a record there.
`devices import PATH… --by SESSION` merges such a file once, by its SHA-256, and leaves the file
untouched. Every entry survives: a held version stays current over a released one, the later
activity wins between equals, the other version is kept in the entry's `importedVersions`, and
two holds of one device leave it uncertain. `status` names a legacy file whose current bytes
were never imported; once it is imported, stop writing it.

A device is released only explicitly. A passed expected release time, an idle screen or a silent
session proves nothing: obtain the release and verify the device's state freshly. A deadline
never releases a device; `status` reports an overdue entry and changes nothing. Release yours
during offline analysis instead of holding an idle reservation. `release` refuses while a run is
active: record its confirmed terminal result first.

Preserve a live or uncertain reservation until recovery confirms its work has stopped, as a
heavy-work guard is preserved. Marking a run active records its process
(`hostProcessIdentifier`) and that process's start time. An active run whose process is gone, or
has another start time, is an orphan; an entry marked `uncertain` or `restoration-unverified` is
uncertain. Neither is reserved again until its owner releases it or
`devices recover KEY --by SESSION --revision N --verified "…"` records an attended recovery: by
the owner or a session they agreed, once the run's processes have stopped and the device's
settings and apps are verified. Recovery refuses while the recorded process still runs, and for
a run sent to the second Mac, while anything there may still use the device
([physical devices and the second Mac](#physical-devices-and-the-second-mac)).

Give every command the device's explicit identifier: the canonical uppercase UDID of a device
you own or have reserved, never a name or `booted`. [Simulator testing](simulator-testing.md)
covers the pool's take, use and release steps.

Agree a window with the owner before any session on a paired iPhone or iPad. A real reservation
and handoff are still required inside that window, and approval of a run covers installing and
launching there, not operating the device by hand. [Device testing](device-testing.md) has the
window, preflight, UI-automation authorization and restoration steps for physical devices.

The reservation records that window as `mac-foreground`'s records the owner's: who approved it,
when, and until a time or `released` (`--approved-by`, `--approved-at`, `--approved-until`, and
`--approval-words` for the owner's words), never past the expected release. The record refuses
to reserve a physical device, a hardware UDID of 8 and 16 hex digits or 40, without one, or to
give an entry one later. Work on the device stops when the window ends: a launcher or a script
calls `devices.require_window(KEY, entry)` before each run, `eng remote --device` refuses, and
`status` reports the device's work as refused. An ended window releases nothing; the session
restores the device and releases it, and a new window from the owner is a new reservation.

### Bounded device runs

Run a command on a physical device through the toolkit's one launcher for a bounded run:

```sh
python3 -m vocaro_engineering.devices run iPhone --owner SESSION --revision N --limit 180 \
  --restoration 60 -- xcodebuild test-without-building … -destination "platform=iOS,id=UDID"
```

A project's launcher calls it from Python, `devices.BoundedRun([KEY], owner, command, revision=N,
limit=…, restoration=…).execute(command, stdout=log)`, and adds its own restoration queries and
release around it, as Pagepour's mobile UI session does. `eng remote --device` runs the same
checks and bound for a run on the second Mac ([below](#physical-devices-and-the-second-mac)).
The launcher:

- requires the session, `--owner` or else `VOCARO_DEVICE_OWNER` or the agent session, to hold
  KEY with no run active, at `--revision` when given, and KEY to name a physical device. It never
  acquires, extends or changes a reservation;
- requires the window the entry records, as above, and refuses once it has ended
  (`devices.require_window`);
- refuses, running nothing, a command that names any device but the entry's: a hardware UDID in
  any case, though not a longer run of hex digits such as a SHA-256, or any device the record
  holds under another key, a simulator among them. Forty hex digits are an older device's UDID
  or a commit's whole SHA-1, so 40 that the current directory's Git knows as an object
  (`git cat-file -e`) name no device; a device's UDID never is one. It cannot recognize a
  device's name or its CoreDevice identifier, so name the device by its UDID;
- runs the command, supervised, under a bound: the reservation's remaining time, to the earlier
  of its expected release and the approval's end, less 105 seconds for the run's own end
  (supervision's wait for descendants that outlive the command, then its SIGTERM, settling and
  SIGKILL waits) and `--restoration` seconds, 60 by default, for the session's restoration
  queries, and at most `--limit`. It refuses a bound under 30 seconds. The bound counts
  `xcodebuild` start-up: on a busy Mac it took nearly five minutes to begin a session whose steps
  then took one. It counts this Mac's waking time, as supervision's timeouts do, so a Mac that
  sleeps through a run lengthens the run by the sleep;
- marks the run active in the entry with its own PID as the run's process, records the command's
  process as the entry's own, which `release` and `recover` wait for, and records the terminal
  outcome in the entry's `run` once the command and its observed descendants have ended: `exit N`,
  which is also its status, a timeout (status 124), a stop (the launcher then ends by the same
  signal) or a command that did not start;
- leaves a run whose end is unconfirmed active and the entry uncertain, for attended recovery;
- updates only its own entry, naming the revision its previous change returned, so an ownership
  change or a concurrent change is refused;
- never releases the device, changes VoiceOver, uninstalls apps or deletes data. The session's
  next change names the revision the launcher's last change returned (`devices show KEY`).

A project's launcher keeps ownership and records `restoration-unverified` when restoration
fails. Read the launcher's terminal output and verify its receipt before reporting a release.

An interrupted test bypasses its own restoration code. After a transport failure or a forced
termination, query the settings independently before releasing; if connectivity prevents
that, record restoration as unverified and coordinate recovery. An uninstall timeout has an
unknown outcome: query the exact bundle's presence, retry removal only if it is still installed,
and verify absence separately.

To release, verify the original accessibility and keyboard settings, the absence of owned test
processes and the measured build identities. Remove only the exact owned app and runner bundles,
verify their absence, release the entry, and tell the other session that the device is released,
including any unresolved restoration state.

### Physical devices and the second Mac

Each Mac's simulators are its own, but the paired iPhone and iPad are reachable from both Macs:
on 2026-10-27 the build host's `devicectl list devices` listed both as `available (paired)`. A
record on each Mac would let a run on each use one device, under reservations neither record
shows the other. So a physical device is reserved only in this Mac's record, the Mac that sends
runs to the second Mac, and the second Mac's record holds only the second Mac's simulators.
The second Mac never reads this Mac's record itself: that would give code running there a way
into this Mac, so a run carries the reservation it may use instead.

Every `eng remote` run first makes the second Mac's record defer physical devices to the sending
Mac's (`devices defer MAC`, kept as the record's `physicalDevicesRecordedOn`, which `status`
reports). A record that defers them refuses to reserve a physical device or to give an entry one,
and names the record that holds them. A hardware UDID, 8 and 16 hex digits or 40, is physical; a
simulator's is a UUID. A record there that already defers them to another Mac, or holds a
physical device, stops the run as a configuration problem for the owner's decision: which Mac's
record holds them changes only by `devices defer MAC --replace` (`--end` ends a deferral), and a
held device is released or recovered first.

A run on the second Mac uses a physical device only under a reservation held here, which it
carries:

```sh
python3 scripts/eng remote admit --device iPhone -- COMMAND
python3 scripts/eng remote run --device iPhone LANE -- ARGUMENTS
```

- `--device KEY`, repeatable, makes the dispatch a [bounded run](#bounded-device-runs) of the
  reservations held here: the session holds each KEY here with no run active, within the owner's
  window, KEY names a physical device, and every device the command or the lane's arguments
  name is one of them; otherwise nothing is sent. `--device-owner` names the session, else
  `VOCARO_DEVICE_OWNER`, else the agent session.
- The dispatch marks each entry's run active with the `eng remote` process as the run's process,
  records the connection's `ssh` as the entry's own process, and records the run's outcome when
  it ends, with the host, the work and the results directory, in the entry's `run`. Its end, by a
  stop, its death or a dropped connection, stops the run there, so a run whose dispatcher vanished
  is an orphan here.
- `devices recover` of an entry whose run went to the second Mac asks that host first, and
  refuses while it lists any process whose command line names the device's UDID, live or left
  over, or any process left over in a checkout or the temporary directory since the run started
  ([processes left over from runs](#processes-left-over-from-runs)). The run's own `offload_host.py`
  names the UDID while the run lasts, since its plan carries the lease, and `xcodebuild` and
  `devicectl` name the device they are given. macOS does not show another process's environment,
  so a process that reaches the device without naming it is not seen. A second Mac that cannot be
  reached keeps the device until it can be, or until the owner recovers it with
  `--offload-unchecked REASON`, which the recovery records.
- The bound starts when the dispatch does, so it counts sending the commits and waiting for
  admission there. Once it passes, by this Mac's clock, the dispatch stops the run there as a stop
  does, and the outcome begins `stopped at its Ns bound`.
- The run reads its reservations from `VOCARO_DEVICE_LEASE`:
  `{"record": MAC, "devices": {KEY: UDID}, "until": END}`, where END is the reservations' end.
  There `devices run KEY -- COMMAND` takes KEY from the lease, since the second Mac's record
  defers physical devices: it checks the devices the command names against the lease, bounds the
  command by END as here, and leaves the recording to the dispatch.
- The device stays reserved after the run. Preflight, restoration and the release are the
  session's, from this Mac, which reaches the device too.
- Without `--device`, a command, or a lane's arguments, that names any hardware UDID is refused,
  whether or not this record holds its device, so a run on a device goes through a reservation:
  reserve the device here and add `--device KEY`. As in a bounded run, a simulator's UUID, a
  longer run of hex digits such as a SHA-256, and 40 hex digits that the Git of the project's
  checkout knows as an object, such as a commit's whole hash, name no device, with `--device` or
  without it. `eng run` keeps a lane whose arguments name a physical device of this record on
  this Mac, and `eng admit --verify` a command that names any hardware UDID.

`eng doctor` reports both Macs' records under `deviceRecord`. It is an error for this Mac's record
to defer physical devices, or for the second Mac's to hold one or to defer them to another Mac;
a record there that defers them to no Mac yet is listed as unchecked until the next run.

The records constrain only work that goes through the toolkit: `xcodebuild` or `devicectl` run
by hand on the second Mac, or Xcode there, reaches the devices without them. A lane that needs a
device says so with `"here"`, so sending one to the second Mac is always an explicit
`eng remote --device`.

## Attended Mac sessions

Mac VoiceOver and foreground UI default to the [hosted runner](hosted-runner.md), not the
owner's Mac. These rules are a method for an attended session on the owner's Mac that the owner
has explicitly authorized; they grant nothing by themselves.

Reserve the owner's foreground before taking it. The `mac-foreground` entry in the
[device record](#device-reservations) stands for the screen, keyboard, pointer, focus and
speech, and names no device. Reserving it records the owner's approval: who approved, when, and
until a time or `released`, with the owner's words if given. No update changes the approval: a
new window from the owner is a new reservation, after a release. Another session sees the holder
in `devices status`, and its reservation is refused while the entry is held.

```sh
python3 -m vocaro_engineering.devices reserve mac-foreground --owner SESSION --revision N \
  --purpose "Mac XCUITest of …" --release-by 2026-10-23T18:00:00Z --approved-by OWNER \
  --approved-at 2026-10-23T15:02:00Z --approved-until 2026-10-23T18:00:00Z --approval-words "…"
export VOCARO_DEVICE_OWNER=SESSION
python3 -m vocaro_engineering.devices check      # at the start and before each action
python3 -m vocaro_engineering.devices update mac-foreground --owner SESSION --revision N \
  --own PID
python3 -m vocaro_engineering.devices release mac-foreground --owner SESSION --revision N \
  --outcome "…" --restoration "…" --frontmost restored
```

A foreground driver calls `devices check`, or `devices.require_foreground(owner)` from Python,
at the start and before each key, activation or `set frontmost`. It refuses unless the session
named by `--owner` or `VOCARO_DEVICE_OWNER` holds the entry, the entry is neither orphaned nor
uncertain, and the approval has not ended; on a GitHub-hosted runner it passes without one.
`eng run` checks a lane marked `"foreground": true` the same way. A passed approval or expected
release releases nothing: `status` reports it, the check refuses further foreground work, and
the session stops, restores and releases. Record each app and test runner the session launches
with `update --own PID`: `release` and `recover` wait until those have exited, and the release
says whether the previous frontmost app was restored (`--frontmost restored`) or the session no
longer owned the foreground (`not-ours`).

The screen and the CPU are separate resources. A foreground timing measurement needs both the
reservation and exclusive admission; a functional UI test needs only the reservation, with its
build admitted as heavy work as usual.

- Approving a mechanism is distinct from the Mac being available now. Announce each start and
  when the owner can resume work, and wait for the go-ahead.
- An explicit, uninterrupted availability interval covers bounded sessions within it without
  repeated requests. Never extend the interval yourself; keep its original deadline and
  restoration requirements.
- Guard every action by the exact app path and PID. Stop global navigation on foreground loss
  or when a helper expires.
- Restore the original settings before returning control.

[Background Mac testing](background-mac-testing.md) and [Mac VoiceOver](voiceover-mac.md)
describe the drivers, key guards and VoiceOver restoration.

### Owner-to-automation handoffs

An owner-local Mac UI run starts from a command the owner types in a Terminal on the Mac whose
screen it uses, and the owner hands that screen to the run. On 2026-10-29 a project's own wrapper
for such a run verified its Terminal caller, handed focus to an ordinary app reserved as its
baseline and verified the handoff. The stock controller's check just before the test body then
found the owner's Terminal in front again and stopped, with zero tests passed, failed or skipped
([vocaro-engineering#68](https://github.com/vocaro/vocaro-engineering/issues/68)). Why focus
returned is not measured. So a run decides afresh before each phase, from what it observes then,
and records the stage it reached:

1. **Context.** The owner types the command on the Mac whose screen it uses, in that Mac's GUI
   session: over SSH nothing reaches the screen. The Mac and the session are established as for a
   keychain ([keychains on the right Mac](#keychains-on-the-right-mac)): the Mac by its
   LocalHostName, the session from the process's audit session flags. The wrapper records the
   caller, the process the owner typed the command in, by PID, executable path and start.
2. **Reservation and admission.** The session holds `mac-foreground` within the owner's approval,
   as `devices check` requires, and the native test runs under heavy-work admission, as any
   `xcodebuild test` does. The approval is the owner-approved interval.
3. **Baseline.** Within that interval the owner approves the baseline, the process the run expects
   in front. The wrapper records its kind, the Mac, its PID, executable path and start, when it was
   approved, and the values of the preferences the run depends on, such as Keyboard Navigation, as
   read then. For an owner-local command, the baseline is the Terminal the owner types it in: the
   caller is then the baseline, so the check before the test body finds it in front without a
   handoff to another app. That check, the controller's own included, asks `decide` below rather
   than refusing any Terminal in front. An ordinary app as the baseline needs a handoff whenever
   the command starts from a Terminal.
4. **Deadlines.** The preflight, the check before the test body, the execution and the cleanup
   each have a deadline, all within the approval.
5. **Decisions.** Before the test body, at the preflight and again just before the body, the
   wrapper reads afresh what the decision needs and asks for it:

   ```sh
   export PYTHONPATH="$VOCARO_ENGINEERING_ROOT/src/python"
   python3 -m vocaro_engineering.handoff decide OBSERVATIONS.json  # 0 proceed, 4 hand off once, 3 stop
   python3 -m vocaro_engineering.handoff ledger RUN.json           # 0 passed and complete, 1 failed, 3 otherwise
   ```

   `decide` adds this Mac's name, this process's session, the session that holds the reservation
   (`VOCARO_DEVICE_OWNER`), the `mac-foreground` entry and whether the run holds heavy-work
   admission, where the observations leave them out, and checks in order, stopping at the first
   that fails:

   - context: a GUI session, on the Mac the baseline was approved on, with the caller recorded;
   - reservation: `devices.require_foreground`'s checks, so a released, foreign, orphaned,
     uncertain or ended reservation stops the run;
   - deadlines: every phase has one within the approval, and the current phase's has not passed;
   - admission;
   - screen: unlocked, with the owner's session on the console rather than the login window;
   - baseline: approved within this interval; its PID still names the same executable and start,
     so a replaced or ended process stops the run; a Terminal baseline is the caller itself; and
     the preferences read now equal those read at approval;
   - foreground: the baseline is in front, by PID, executable path and start. An unknown modal
     in front stops the run and is left for the owner. The baseline's app running as another
     process stops it, for the owner to approve the new one. With an ordinary-app baseline, the
     caller's Terminal in front at the preflight, before any handoff, asks for the one handoff:
     activate the baseline by its PID, verify it, and decide again. The Terminal in front after
     that, or just before the test body, stops the run, since a second handoff would take focus
     back to make a check pass. Any other app in front is the owner's own use: the run stops and
     takes nothing back.

   A stop dispatches nothing and is unmeasured. The decision looks at, activates and focuses
   nothing itself: it judges what the wrapper read.
6. **Counts.** `ledger` judges the run from its decisions, its native test counts and its
   cleanup. Zero tests executed, or a refusal before the test body, is unmeasured, and so is a
   failure before dispatch; a test dispatched after a stop counts for nothing. Failed tests, or
   another number executed than expected, fail the run.
7. **Cleanup and release.** The run's own processes have ended, the frontmost app was restored or
   recorded as no longer the session's, and `mac-foreground` is released, as `devices release`
   records them. The run is complete only once all three are verified; a passed run whose cleanup
   is not verified is unmeasured.

Keep each of these a separate observation in the run's record: the owner's word that a command
finished, an unlocked desktop, valid signatures and a verified focus handoff. None is a native UI
control; only native test counts above zero are. Signing that succeeds in a GUI session is not
key access over SSH ([keychains on the right Mac](#keychains-on-the-right-mac)), and a capture
permission preflight is not a captured bitmap
([capturing a background window](background-mac-testing.md#capturing-a-background-window)).

- A Terminal baseline cannot tell the owner's typing in that Terminal from the run's own use:
  the owner leaves it alone for the approved interval.
- A handoff changes no guard: drivers still call `devices check` before each action, and TCC,
  keychain access control, lock settings and host configuration stay the owner's.
- Not measured: a native UI run under this procedure, with a Terminal baseline or an ordinary
  app's, and why focus returned to the Terminal in the run above
  ([vocaro-engineering#68](https://github.com/vocaro/vocaro-engineering/issues/68)).

## Copies that share a bundle identifier

Several sessions may run their own Debug builds of one app, all with one bundle identifier. A
tool that picks an app by bundle identifier, such as computer use or AppleScript's
`application id`, can reach another session's copy and its windows. List the running copies'
paths with `ps -Ao pid,args`, and act on or quit only your own, by path and PID. The same holds
for Xcode: with two Xcodes running, `application id "com.apple.dt.Xcode"` can reach the wrong
one, so address Xcode by its path.

## Process supervision

Keep three deadlines distinct: the tool's output wait, the connection or launch timeout, and the
test's behavior or observation deadline. A tool timeout is an observation timeout. A tool that
returns a session handle may leave the command running: resume the handle and inspect its log
and processes rather than launching a duplicate. Wait for a confirmed terminal result before
starting dependent tests or pushes. Never infer termination from a missing parent or an expired
observation wait.

Supervise the process group you own. The toolkit's `processes.supervise`, which `eng run`,
`eng admit` and the resource CLI use (`eng run` with a unique log per lane under
`.build/engineering/LANE/`), follows these rules:

- Start the command in a new session and process group. Capture the root's start identity
  (`ps -o lstart=`) before `poll()` can reap it; once reaped, its PID cannot seed adoption or
  group signals, even if another process reuses it.
- Signal the group and each observed descendant individually, comparing each descendant's start
  identity: Swift drivers can start other process groups and sessions.
- A refused signal never ends supervision. Darwin answers `killpg` with EPERM when the group's
  only member is the exited root that has not been reaped yet, and an unsignalable descendant is
  still awaited.
- A command waits for its observed descendants before its status counts. Periodic sampling
  cannot prove that a fast child did not orphan itself between samples, so an uncertain run
  raises and admission keeps its guard for inspection; a confirmed terminal exit keeps its
  status and releases normally. A stop or a timeout whose command and observed descendants all
  end releases too, since its proof is the same as a confirmed exit's (decided 2026-10-22 for a
  stop, 2026-10-23 for a timeout).
- A nonzero exit, a stop or a timeout is confirmed only by a final census. At each sample the
  supervisor records the process group (from `ps`) and the session (from `os.getsid`, since
  `ps -o sess` prints 0 on macOS) of every live observed process, beside the root's own, which
  are its PID. Once the root and every observed descendant have ended, any live process in one
  of those groups or sessions leaves the run uncertain, and the error names each by PID, group
  and session. An ID that is now a live process's PID is skipped: the kernel never gives a new
  process a PID that still names a group or session, so that ID was taken over after the
  command's group or session ended. The candidates are the groups and sessions seen, never
  every observed PID: one push recorded 11,027 process identities, and PIDs advanced about 87 a
  second on the 18-core host, wrapping the PID space about every 19 minutes (2026-10-23, macOS
  27.0). The census adds one `ps`, as each sample takes, and a `getsid` sweep: 0.17 ms for 892
  processes on that M5 Max (2026-10-26, macOS 27.0.1). It cannot see a child that moved to a
  session of its own and was orphaned between two samples; on the second Mac, the next run there
  lists one that runs from a checkout, the temporary directory or a simulator
  ([processes left over from runs](#processes-left-over-from-runs)). An unrelated process that
  takes over a recorded ID and leaves its own children in that group or session reads as a
  survivor, which errs toward inspection. A zero exit has no census, so that a daemon a passing
  command starts on purpose does not stop the host's heavy work. Until 2026-10-26 no run had a
  census, and a failed command's unobserved orphan ran on after its admission was released.
- Inside an admission that its own process holds, the supervisor appends what it sees to the
  admission's process record ([lock reclamation](#lock-reclamation)): the command before it
  starts, its root once started, each process with its start and each process group and session
  at the first sample that sees it, and the command's confirmed end. When the admitting process
  is killed before it settles its entry, `eng admissions recover` runs this census against the
  record.
- A push keeper ([checkout provisioning and publication](#checkout-provisioning-and-publication))
  supervises its own commands and applies the same census to a command that fails on its own,
  with a positive status and no stop. When the census finds nothing, the keeper releases its
  heavy-work admission but keeps the checkout's transaction lock, its owner record showing
  `"failed": true` and `"childrenUncertain": false`, so the next push from that checkout is
  refused until someone has read why this one failed and removed the lock. A stop, a command
  ended by a signal, a killed keeper or a process the census finds leaves both in place, the
  admission entry marked uncertain, even after the observed children drain. A command that
  passes has no census there either.
- Once the root exits, its observed descendants have 60 s to end. The supervisor lists any still
  running on stderr, as `ps -o pid,ppid,pgid,lstart,args` rows with the parent and group each
  had when first seen, and stops them: SIGTERM, then SIGKILL 10 s later. The root's status counts
  once they are gone; a process that survives SIGKILL is still awaited. A lane's `timeout` bounds
  only the root, so a root that exits in time keeps its status through the grace. Agents
  reported Xcode's `DTServiceHub` outliving an ad hoc `xcodebuild … test` on a fresh iOS
  simulator by 10 to 15 minutes, ignoring SIGTERM, until they killed it by hand (EPUBLib, macOS
  27.0, Xcode 27.1, October 2026).
- A command that cannot start, such as a mistyped name or a script without its executable bit,
  raises `NotStarted` and releases admission. `Popen` reaps the child whose exec failed before
  it raises, so nothing is left to inspect; keeping the guard would stop every session's
  admission over a typo.
- `eng admit`, `eng run` and the resource CLI carry out SIGTERM, SIGINT and SIGHUP as a stop
  ([lock reclamation](#lock-reclamation)). The supervisor forwards the signal to the root's group
  and to each observed descendant, sends SIGKILL to what remains 10 s later, or once a nested
  admission has settled (below), and raises `Interrupted` once the root and every observed
  descendant have ended and the census finds nothing. A later signal does not hasten SIGKILL,
  because a task stop can deliver its SIGTERM twice, to the process group and to the process. A
  stop sent to the whole process tree can end a sample's own `ps`, which is then asked again. A
  signal the caller ignores, as `nohup` ignores SIGHUP, stays ignored.
- A lane's `timeout` that expires while the root runs is carried out the same way with SIGTERM:
  once to the root's group and to each observed descendant, SIGKILL to what remains 10 s later,
  and `TimedOut` once all have ended and the census finds nothing. The run is unmeasured
  (exit 3), never green, and admission is released; a process still running 5 s after SIGKILL,
  or one the census finds, leaves the guard uncertain. Until 2026-10-23 the timeout sent SIGTERM
  again every 50 ms, never sent descendants SIGKILL, and left the guard uncertain even when
  everything had ended.
- SIGKILL waits for a nested admission to settle. When SIGKILL falls due, after a stop, a timeout
  or the grace, and the root or an observed descendant is an admission owner (its PID and start
  time are an owner's in the guard), the supervisor sends no SIGKILL until every such owner has
  ended, for at most `SETTLE_WAIT` (30 s) more. Such an owner is an `eng admit` or resource CLI
  run inside a command that holds no admission, such as a light lane's script, so it has a guard
  entry of its own. It has received the signal too, so it stops its own command, with its own
  10 s to SIGKILL and 5 s after, and then removes its entry, or keeps it uncertain if a process
  of its command is left. Then SIGKILL reaches what remains, an owner that never settled
  included, which leaves its entry for attended recovery; a stopped or timed-out run is
  uncertain if any process is still running 5 s after that SIGKILL. Until 2026-10-27 SIGKILL
  came 10 s after the stop whatever ran, so a light lane whose script admits its own work, as
  BreezEPUB's book-opening lane does, could end its `eng admit` while Xcode's tools were still
  ending, and the entry left behind refused every session's heavy work until the guard was
  recovered by hand.
- After the first signal of a stop, a timeout or the grace, a process first seen later receives
  that signal once, unless its parent is such an admission owner: the owner forwards the stop to
  its command itself, and its other children are its own `ps` samples. While each new one was
  sent SIGTERM, three in a row ended, the owner's supervision failed and its entry stayed
  uncertain in 4 of 144 stops of a nested admission, run 12 at a time (2026-10-27, macOS 27.0.1,
  Python 3.14.6).

Terminate only an observed launch PID whose current executable still matches; an unknown or
reused PID never authorizes termination, and a cleanup error keeps the first failure.

A lock keeper started independently of the invoking client survives the client's loss and holds
its locks until its command group drains. Cancellation sends SIGTERM without automatic
escalation, so a nonresponsive child can leave a live keeper holding the locks until attended
recovery. Never remove a live keeper's locks.

An `xcodebuild` UI-test root and the shared supervisor finish at different times. Follow the
root to a terminal test result and copy the evidence. Then shut down only the owned simulator,
which ends the remaining simulator descendants sooner than the supervisor's grace, and follow
the supervised command until it exits and admission is released. Root exit, an observation
timeout, a passed deadline or the shutdown alone does not release a reservation; when the
supervisor's outcome is uncertain, keep the reservation for recovery and launch no replacement
run against it.

Tests of supervision code bound stalls rather than durations and start a timeout under test
only once the real descendant has published its identity ([app testing](app-testing.md)).

## Long-running measurements

Background a long run with its output redirected to a log.

- Agent shells can start with an empty PATH. Export it first, including Homebrew's `bin` where
  tools such as `uv` live, and invoke binaries by absolute path. Redirect to a file rather than
  piping through `tail` or `grep`: a missing pipe binary kills the whole pipeline.
- An empty log is buffering until shown otherwise: redirected output is block-buffered (about 4
  to 8 KB). Check the processes, twice, since a run alternates working and reporting.
- In zsh, the shell agents' commands run in, `status` is a read-only parameter. `status=$?`
  after a run fails, stops the wrapper and loses the run's exit code: a 48-minute gate's result
  had to be read back from its log (October 2026). Capture it as `rc=$?`.
- Apple's `git grep -E` has no `\b`: a pattern using it matches nothing and exits 1, exactly as
  a search that found nothing does. A sweep for British spellings read that silence as a clean
  tree (October 2026). Use `git grep -w`, or `grep -E`, which honors `\b`.
- Wait for a run by its own completion signal, a line its wrapper appends or a file it
  publishes, never by `pgrep -f PATTERN` from a shell, whose own command line matches the
  pattern.
- On-device FoundationModels generation burns CPU in `TGOnDeviceInferenceProviderService` and
  `modelmanagerd` while the test runner sits near idle. Private Cloud Compute generation shows
  about 0% local CPU for the runner and the local model server alike, so check their connection
  instead (`lsof -nP -iTCP:PORT` shows an `ESTABLISHED` pair) and the server's health response
  (macOS 27, October 2026).
- The first Vision text recognition in a process cost about 55 seconds of model preparation and
  about 0.35 seconds a page after it (macOS 27, October 2026). Time warm-up apart from
  throughput.
- Run one generative-model workload per machine. Two processes driving one Mac's on-device model
  both sat at about 0% CPU with no output for many minutes, and concurrent Core ML and
  FoundationModels work made an embedder return silently wrong vectors
  ([embedding correctness](../knowledge/ml/embedding-correctness.md)). Admit the run with
  `--model`, which holds the host's model lock while builds and tests run beside it
  ([model admission](#model-admission)), and a run that is timed or compared with a baseline with
  `--exclusive`. Before starting, look for other `swiftpm-testing-helper` and `xctest` processes,
  including another worktree's suite, and take a process listing during any wall-clock
  measurement. Whether heavy work beside on-device generation slows it or changes its output is
  not measured.

[Measurement](measurement.md) covers what to record.

## Long pre-push checks

Git's SSH transport holds the connection that read the remote refs open for the whole pre-push
hook. During long gates GitHub closed that connection despite an SSH `ServerAliveInterval`
keepalive, and the push ended on SIGPIPE after every gate had passed (October 2026). Push over
HTTPS instead: it reads the refs before the hook and sends the pack in a separate request after
it, so no connection has to outlast the gates.

A per-command global configuration publishes over HTTPS without changing the repository's or the
user's configuration. Pass it through the environment so the hook and every nested gate see it:

```sh
cat > "$SCRATCH/push.gitconfig" <<'EOF'
[include]
	path = ~/.gitconfig
[url "https://github.com/"]
	insteadOf = git@github.com:
[credential "https://github.com"]
	helper = !gh auth git-credential
EOF
GIT_CONFIG_GLOBAL="$SCRATCH/push.gitconfig" git push origin SHA:refs/heads/main
```

HTTPS has failure modes of its own:

- A pack larger than `http.postBuffer` (1 MiB by default) is streamed with chunked encoding, and
  GitHub answered a 116 MB chunked pack with HTTP 408 after both lanes had passed. The same push
  with `http.postBuffer` at 1 GiB and `http.version` at `HTTP/1.1` published a 208 MB pack.
  Measure the pack first and set the buffer above it:
  `git rev-list --objects origin/main..SHA | git pack-objects --stdout | wc -c`.
- Because the refs are read before the hook, another push to `main` while the gates run makes
  GitHub refuse the update afterwards
  (`cannot lock ref 'refs/heads/main': is at NEW but expected OLD`). Fetch; if the gated commit
  still descends from the new `origin/main` (`git merge-base --is-ancestor origin/main SHA`),
  push it again, and otherwise integrate and run the gates on the new tree. Push the gated
  commit by name (`SHA:refs/heads/main`) rather than `main`, so a retry never publishes a local
  `main` another session advanced after the gates ran. In zsh, brace a variable in that refspec
  (`"${sha}:refs/heads/main"`): an unbraced `$sha:refs/…` reads `:r` as a history modifier and
  mangles the refspec.

Follow the owned hook to its terminal result before starting more work. Record the exact commit,
the clean tree and each complete gate result. A transport failure does not establish a test
failure, and interrupted or missing checks never count as passed. A retry follows the
repository's hook policy and publishes only the verified commit. Whether a documentation-only
commit may skip the hook is that repository's policy, not a shared default.

## Checkout provisioning and publication

A fresh checkout or worktree has none of the ignored inputs its gates need. Provision them
idempotently: fill gaps only, verify each input against a committed identity, and report a
present input whose bytes drifted rather than overwriting it. A `--check` mode reports without
changing anything.

A Claude Code session started in a worktree reads the project configuration from the checkout
the worktree was made from, so a `SessionStart` hook's `$CLAUDE_PROJECT_DIR` and working
directory are that checkout; the session's own directory arrives only as the `cwd` field of the
hook's input (observed October 2026). Read that field and hand over to the worktree's copy of
the provisioning script.

Reuse a large input from another registered worktree of the same repository before fetching it:

1. Open the donor's regular file without following a symbolic link.
2. Verify its committed byte count and SHA-256.
3. Make an APFS clone in private staging on the destination volume.
4. Verify the staged clone the same way, then publish it create-only.

Donor and destination keep independent file identities. A missing, drifted or unusable donor, a
different volume or unsupported cloning falls back to the fetch path; report verified donor
reuse separately from fetch completion.

Link a shared ignored directory with `ln -sn TARGET LINK`. In a reused worktree that already
holds the link, a bare `ln -s` follows it and writes a link inside the target pointing at
itself; `-n` refuses with "File exists".

Keep writable build products and Python environments private to one checkout, at a stable path,
so warm builds stay warm. An APFS-cloned `.build` at a different source path recompiled with
stale-path warnings (6.6 s against 0.7 s for a same-path warm build), and adding compiler CAS
flags directly to a normal SwiftPM gate command failed planning (both on Xcode 27.1, Swift 6.4,
October 2026).

A pre-push hook runs its gates over the working tree, not over the commits being pushed, so a
tested publication comes from a clean checkout of the exact commit. Never push a shared tree
that holds another session's edits. A persistent push checkout makes this cheap:

- a separate, canonical linked worktree at a stable path, detached, with its own ignored inputs
  and build products (`git worktree add --detach PATH origin/main` creates it);
- advancement only to a descendant of its HEAD, by detached checkout; it refuses dirty tracked
  bytes or modes, a differing index, nonignored untracked files, symbolic paths and backward or
  divergent moves, and never resets or cleans;
- a keeper that holds the checkout and host gate locks from before it moves the tree until the
  remote acknowledges the push, and refuses an inherited variable that redirects the host lock;
- nested lanes that take their own temporary locks while the host lock stays held, so test
  fixtures keep real locks rather than an inherited opt-out;
- Git variables that a hook exports for its repository (`GIT_DIR`, `GIT_INDEX_FILE` and the
  others) cleared before any command touches a nested repository;
- HEAD, the index tree, every tracked byte and mode and the nonignored untracked files checked
  against the frozen commit before and after each lane;
- success reported only with a commit and tree receipt; a no-op push without a hook receipt is
  not a qualified publication.

## Toolchain selection

Use a full Xcode, not the Command Line Tools. The Command Line Tools ship the same Swift, so a
version check passes, but they lack `coremlcompiler` and cannot build test targets;
`xcode-select -p` shows which is selected. Name the stable path, `/Applications/Xcode.app`,
rather than an installed version, and prefix direct `swift`, `xcrun` and `xcodebuild` commands
with it:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun xcodebuild -version
```

Name a beta or second Xcode by its own path in the same way for every command that needs it, and
record `xcodebuild -version` and `sw_vers` with each result. Installing or first launching
another Xcode can restart the simulator service under every session;
[simulator testing](simulator-testing.md) covers what that does to running simulators.

## Inspecting system code without starting it

Use `dyld_info -disassemble` on a framework's path under `/System/Library`, which prints its
code under its method names even for a framework that exists only in the dyld shared cache, or
`lldb -b -o "target create BINARY" -o "disassemble -n METHOD"` without a run command for a
binary on disk. `target create` refuses a framework that is only in the shared cache as one that
"does not exist". Neither names the selector a stub call goes to or the characters of a string
address; a reader that maps the shared cache as its own process does can annotate both. A
framework's localized `.loctable` property lists map a word heard or shown to its key, which
leads to the code that uses it ([Mac VoiceOver](voiceover-mac.md) applies this to VoiceOver). To
see calls made into code you own, log from that code.

Never start or attach a debugger to answer a framework question on the shared workstation.
Developer Mode is off there (`DevToolsSecurity -status`), so a debugger launch raises an
administrator-password dialog that takes the keyboard from the owner and every session. If one
appears, stop your processes, leave the dialog for the owner and say so. Sampling your own child
(`sample "$(pgrep -P $!)"`) needs no password; never sample another session's process.

## Lock reclamation

Four kinds of marker coexist, and they are reclaimed differently:

- The legacy shlock gate file (`/tmp/studywright-gate-suite.lock` by default) holds a PID, and
  `shlock` reclaims it once that process is gone, so a killed gate does not strand the next one.
- A guard (`<gate lock>.warm-push-guard`, which admission and the push keeper publish) records
  each owner's PID, process start, label, admission time and an uncertainty flag; beside it,
  each entry's process record lists what supervision saw of that owner's commands. It is never
  reclaimed by PID or by time, because a dead keeper's compilers or simulator children can
  outlive it. Gates check the guard before trying `shlock`, and admission refuses to proceed
  while any guard owner is dead, foreign or uncertain.
- The exclusive queue (`exclusive-queue.json` in the slot directory, changed only under its
  `coordination.lock`) records each waiting exclusive request's PID, process start and label, in
  arrival order. A queued request holds no slot and has started nothing, so admission drops one
  whose process has ended or whose PID has another start time, and a request that times out or
  is stopped takes itself out ([heavy-work admission](#heavy-work-admission)).
- The device record's lock (`device-handoff.json.lock` beside the record) is an `flock` held for
  one change. The kernel releases it when its process ends, so it needs no reclamation, and its
  file is never removed. A reservation in the record ends only by its owner's release or an
  attended recovery ([device reservations](#device-reservations)).

A job leaving admission changes only its own guard entry, whatever state the other owners are
in: a clean exit, or a stop or timeout that saw all of its processes end, removes the entry,
and the guard with its last one; any other exception marks it uncertain. It never changes or
drops another owner's entry. Until 2026-10-22 a job that left while another owner was dead
changed nothing, so its own entry died with its process: one lane killed by its agent stranded
a run that finished normally beside it, and the host admitted no heavy work for 80 minutes,
until the guard was recovered by hand.

`eng admit`, `eng run` and the resource CLI carry out SIGTERM, SIGINT and SIGHUP as a stop. A
stop while queued leaves nothing to release. Once admitted, the job first marks its entry
uncertain, in case SIGKILL follows, then stops its command
([process supervision](#process-supervision)). When the command and every observed descendant
have ended, and the final census finds nothing left in their process groups and sessions,
admission removes the entry and the process ends by the signal it received; if any is still
running 5 s after SIGKILL, or the census finds one, the entry stays uncertain for attended
recovery. SIGKILL itself cannot be caught: a killed job's entry stays, dead, for attended
recovery. Until 2026-10-22 none of these signals was handled. SIGTERM and SIGHUP ended the
admitting process at once, leaving its entry dead and its command, in its own session, running
unsupervised; SIGINT stopped only the command's process group and left the entry uncertain.

A job admitted inside another supervised command, as when a light lane's script runs `eng admit`,
is stopped in this order. The outer supervisor sends the stop's signal to the lane's processes,
the nested `eng admit` and its command among them, and leaves the nested job's later children
to it. The nested job marks its entry uncertain and stops its command: the signal, then SIGKILL
10 s later. Once the command and every observed descendant have ended and its census finds
nothing left, it removes its entry and ends by the signal; if any process is left, it keeps the
entry uncertain and ends the same way. The outer
supervisor sends SIGKILL to nothing while the nested job runs, up to 30 s beyond its own 10 s,
and then to whatever remains ([process supervision](#process-supervision)). Only a nested job
that has not settled in that time is killed with its entry in place.

An agent's task stop is such a signal: Claude Code's TaskStop sends SIGTERM to every process
descended from the task's shell, the command in its own session included, and SIGKILL 1.5 s
later. A command that ends promptly on SIGTERM is released well within that: a probe whose
command had a child in its own session had its entry removed 69 ms after the SIGTERM (2026-10-22,
macOS 27.0, Python 3.14). A command that takes longer, as an `xcodebuild` tree can, is killed
with its admitting process and leaves its entry uncertain. To end an admitted command early, stop
the command's own processes, which supervision then follows to a terminal result, rather than
the task.

In Claude Code, neither a foreground timeout nor a subagent's end stops a command. A command that
outlasts its tool timeout moves to the background and runs on, and the session is told when it
ends; a subagent that ends its turn leaves its background commands running. In each probe the
command and its child in its own session ran to their own end without receiving a signal
(2026-10-23, Claude Code 2.1.303 in the desktop app, macOS 27.0). A timed-out foreground command
is therefore still the run: follow it to its terminal result rather than stopping it or starting
another. Start an admitted command that can outlast the foreground wait in the background, and
never stop its task to retry.

A fork, the Agent tool's `fork` type, ends the same way. A fork that started the probe in the
background and ended its turn was reported as waiting on its own background work, and the
command and its child in its own session ran their full 240 s without receiving a signal
(2026-10-29, Claude desktop app running Claude Code 2.1.309, macOS 27.2). In that bundle the
`fork` type runs through the same `runAgent` as other agent types, not the internal
`runForkedAgent`, whose teardown kills the agent's shell tasks. `runAgent`'s cleanup skips that
kill only for an agent that ran in the background, finished without being stopped and still owns
a running background command. So a subagent run in the foreground, or one stopped, has its
background commands stopped at its end; that was read from the bundle, not measured.

Stopping a subagent with TaskStop stops every command it started, background commands included,
as a task stop does. In each of three probes the command and its child in its own session
received SIGTERM in the same millisecond and ended by SIGKILL 1.48 s later. Twice the subagent was
waiting on a foreground command with a background command beside it, and both were stopped; once
it had ended its turn and was waiting on its background command alone (2026-10-27, Claude desktop
app 3.6556.2 running Claude Code 2.1.308, macOS 27.0.1, load average about 5). Claude Code 2.1.308
ends the stopped subagent's turn, which stops its foreground command, then stops each of its shell
tasks still running. An admitted command that has not ended 1.5 s after the SIGTERM is killed with
its entry in place. On 2026-10-22 a coordinator at a usage limit stopped ten subagents this way.
Three were inside `eng admit`, running a VoiceOver simulator session and two `swift test` runs, and
their entries kept every session on the host from heavy work for more than two hours, until the
owner approved removing the guard. The same subagents ending at the account's usage limit
(HTTP 429) left nothing behind three times, as a subagent's end stops nothing. To wind subagents
down, ask each to start nothing new, stop only its commands still waiting for admission, which
hold no entry, and let its admitted commands finish. On 2026-10-23 that left the guard empty, and
a gate already running released normally. Whether a person stopping a subagent, rather than a
coordinator's TaskStop, sends the same was not measured.

Interrupting a turn, and archiving or deleting a session, stop commands as a task stop does. An
interrupt reaches only the turn's foreground command; the session's background commands run on.
Archiving or deleting a session from the sidebar stops its background commands. The app's archive
tool refuses a session whose background command is running, but the sidebar does not. A command
and its child in its own session received SIGTERM within 7 ms of each other and ended by SIGKILL:
1.40 and 1.48 s later after an archive, 1.04 and 1.10 s after a delete, within 2.5 s after an
interrupt. In a second interrupt only the command received SIGTERM, and SIGKILL ended it 1.0 s
later; its child in its own session received nothing and ran to its own end. Background commands
ran to their own end through both interrupts (2026-10-23, Claude desktop app 3.6556.1 running
Claude Code 2.1.308, macOS 27.0; the interrupts were sent with the app's session stop tool, which
it describes as pressing Stop).

Claude Code 2.1.308's code for stopping a shell command explains the second interrupt. It signals
the shell's process group and each descendant that one `ps -A` scan lists, but abandons a scan
that takes longer than 500 ms and then signals the group alone. SIGKILL follows 1.5 s after the
stop begins, to the group, to the descendants found and to a fresh scan from the shell, which has
usually exited by then. The host's load average was above 100 during these probes. `eng admit`
runs in the shell's process group, so it always receives the SIGTERM and stops its command, but
it has 1.0 to 1.5 s before its own SIGKILL, and nothing escalates after that. A command that has
not ended by then leaves its entry uncertain and, if the scan missed it, runs on unsupervised.

Codex does not time a command out. Its shell tool returns once the requested wait passes and
leaves the command running, unsignaled, under a session handle. When the Codex session ends, the
command, which leads its own session, is killed with SIGKILL and no SIGTERM first; its child in
its own session receives nothing and runs on. `codex exec` ends its session with its turn: in two
runs the command was killed without a logged signal, in the watched one 5 ms after the turn
completed (2026-10-23, Codex 0.163.0-alpha.11.2 as ChatGPT's Codex Desktop bundles it, run as
`codex exec`, macOS 27.0). An `eng admit` killed this way never marks its entry uncertain: the
entry stays, dead, and its command runs on unsupervised. A Codex agent therefore follows an
admitted command to its terminal result before ending its turn. How long a Codex Desktop thread
keeps a command after its turn ends, and what quitting the Claude desktop app sends, were not
measured.

Recovery of a guard entry is attended, and `eng admissions recover` carries it out. The first
command below lists each entry that refuses admission, dead or uncertain, with its identity, label,
PID and how long ago it was admitted (for an entry an older toolkit wrote, how long ago its owner
started), whether its owner still runs, and whether it has a process record. The second recovers
one entry, named by its owner's PID or by its identity:

```sh
python3 scripts/eng admissions recover
python3 scripts/eng admissions recover 56766
```

It removes that entry alone, and the guard with its last entry, deletes the entry's process record
and logs a `recovered` line ([admission log](#admission-log)), only when all of these hold:

- Its owner has ended: its PID is gone, or `ps` reads another start there. A live owner's entry is
  never touched, uncertain or not, named by PID or by identity: the owner removes it, or leaves it,
  when it ends. A `ps` that cannot say leaves the owner taken as running.
- It has a process record that reads whole and names the process of each command it has not
  settled.
- The census a run ends with ([process supervision](#process-supervision)), run against that
  record, finds nothing: no recorded process still runs with its recorded start, and no live
  process is in a process group or session the commands were seen using, but for an ID that is now
  a live process's PID.

Otherwise it changes nothing, exits 1 and says why: each process still running, by PID with why it
counts and a `ps` listing, or what to check by hand. Stop or wait for what still runs, then run it
again. It leaves a foreign guard alone too, such as a legacy keeper's (below).

The process record is `<guard>.IDENTITY.jsonl`, beside the guard, named by the identity that keys
the entry. The toolkit's supervision in the admitting process (`eng admit`, `eng run`, the
resource CLI, and `processes.supervise` inside an `Admission` the same process holds) appends JSON
lines to it, each naming one command by a key of its own: `argv` before the command starts; `root`,
its PID, once started; at each sample that sees something new, the `processes` first seen, as
`[PID, start]`, and the process groups and sessions first seen, as `spaces`; and `settled` once the
run confirms that the command ended, by exiting, or by a stop or timeout that saw it and every
observed descendant end and whose census found nothing. Recovery checks no settled command, so a
daemon a passing command starts on purpose does not keep the entry, as it does not keep the run
uncertain. Nested work in another process reuses the admission and writes nothing to the record,
since the outer supervisor sees its processes. Release deletes the record after the entry; an
uncertain end keeps it. A line that cannot be written deletes the record and ends recording, with
`heavy: the process record was not written`, so recovery never trusts a record that lacks a line.

An entry without a process record is left for a check by hand: one an older toolkit wrote, one
whose owner ended before it began a supervised command, and one whose caller runs its own commands,
as StudyWright's push keeper does with `Admission` directly. Inspect its recorded owner, that
owner's command processes and any reserved tree; stop or wait for all of the transaction's
children; then remove the entry by hand. A push keeper's `owner.json`, in its checkout's transaction
lock, lists each command the keeper ran with its PID, process group, start time and observed
descendants. A free owned descriptor or a later foreign acquisition, checked against the recorded
process birth and descriptor nonce, establishes that an old lock was released. Preserve every
foreign reservation. Time passing alone establishes none of this. Recovering the guard those
subagent stops left (2026-10-22) found all three recorded PIDs dead, no swift-frontend, xctest,
xcodebuild, DTServiceHub or simctl process left, and no slot file held open. The VoiceOver run had
left two booted simulators, which its own logs proved its, so they were deleted by UDID. On
2026-10-27 a persistent push keeper that an agent's task stop ended ten minutes into its gate had
left nothing running: a census against its `owner.json` checked 22,992 PIDs with their start
times and 8,088 process groups and found no survivor, and the host still admitted no heavy work
until the entry was removed by hand.

A push keeper's guard is `{"keeper": PID, "checkoutLock": PATH}`. Admission reads it as foreign
and waits, so while it stands no session on the host is admitted to heavy work. `PATH/owner.json`
lists each command the keeper ran, with its PID, process group, start time and observed
descendants, plus `childrenUncertain`. Check each recorded process by PID and start time. This
was observed when a studywright warm push's keeper died with uncertain children (2026-10-21).

### Probing stop signals

The stops above were measured with `siglog.py` and `watch.py`, kept with the scripts that start
and read them in [`src/python/StopSignals`](../src/python/StopSignals), standard library only.

- `siglog.py LOG child SECONDS` logs each SIGTERM, SIGINT and SIGHUP it receives and runs on, and
  starts a child that leads a session of its own and does the same. Each writes `exiting` after
  SECONDS. Any other signal, SIGKILL among them, ends a process without a line.
- `watch.py LOG OUT SECONDS` polls `kill(pid, 0)` every 5 ms for each process LOG names and for
  the parent of its `child`, the command's shell, and writes when each is gone. It ends when all
  are gone or after SECONDS.
- `detach.py COMMAND …` starts COMMAND in a session of its own, with launchd as its parent and
  its input and output on /dev/null, and prints its PID. A stop that signals the caller's process
  group or session, or the descendants a scan finds from its shell, does not reach it.
- `report.py DIR` reads each `NAME.log` with the `NAME.watch` beside it, and prints each probe's
  lines in seconds from the first signal logged and what ended each process.

Start the watcher detached, where the action under test cannot reach it, then have the agent or
tool under test run the probe as its command, so the probe's processes are that command's:

```sh
K=~/Development/vocaro-engineering/src/python/StopSignals D=/private/tmp/stop-probe/NAME
mkdir -p "$D" && python3 -I "$K/detach.py" python3 -I "$K/watch.py" "$D/probe.log" "$D/probe.watch" 1800
python3 -I "$K/siglog.py" "$D/probe.log" child 300 && true    # run by the command under test
python3 -I "$K/report.py" "$D"                                # once the action is done
```

`&& true` keeps a shell given the line with `-c` as the probe's parent, as Claude Code's Bash tool
is; zsh otherwise replaces itself with a command that ends its string. In the report, a process
that got a signal and went without `exiting` was killed after it, and the gap between the two is
the stop's escalation to SIGKILL; one that went with no signal logged was killed by SIGKILL alone;
a `session-child` that ran to its own end while its `child` was signaled was missed; and both
running to their own end with no signal logged means the action stopped nothing. A parent that
outlives the probe, such as an agent's long-running process, keeps the watcher until SECONDS.

## Ending a coordinated command

Use the supplied `eng run` or resource CLI for commands. They return the child's status after
leaving admission normally. If using Admission directly, leave the context before raising
SystemExit for a nonzero result. An exception inside the context retains its uncertain guard,
by design, because the coordinator cannot infer whether that exception left children running.
Admission used directly handles no signals. To stop as the supplied commands do, enter
`processes.forwarding_signals()` around it and pass `on_stop=admission.interrupted` to
`supervise`. `supervise` called in the process that holds the admission writes its process
record; a caller that supervises its own commands writes none, so an entry its killed process
leaves is recovered by hand ([lock reclamation](#lock-reclamation)). The record holds only what
`supervise` ran, so a caller that also starts long-lived processes another way inside the
admission leaves recovery blind to them: run them through `supervise` too.

Legacy shell gates authenticate inherited exclusive admission for their exact lock namespace.
The coordinator does not export a lock opt-out into the suite: regression fixtures retain
independent real locks. A process wrapper must forward the admission descriptors, as the
shared process supervisor does; copying the token environment alone does not transfer ownership.
