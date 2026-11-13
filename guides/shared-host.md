# Shared host

These mechanisms and lessons apply when the stated capability is present. Original
measurements establish their recorded platform and workload; they do not establish
universal behavior on later operating systems or in a different application.

## Heavy-work admission

On the shared host, builds, `swift test`, `xcodebuild test`, gate suites, corpus lanes and
conversions run through heavy-work admission: one host-wide resource namespace with bounded
concurrency. Run a configured lane with `python3 scripts/qk run LANE`; a lane whose `resource`
is `heavy`, `light` or unset takes one slot, an `exclusive` one the host, and a `self` lane runs
directly, since its script admits each of its parts. Every lane waits its turn, first come, first
served: a short check, such as docs or spelling, waits for a slot like any other lane (the owner's
decision, 2026-11-07, [vocaro-engineering#200](https://github.com/vocaro/vocaro-engineering/issues/200)).
That includes the merge queue's spelling check in a project with no `spelling` lane, which `qk run spelling`
runs as a lane of the toolkit's (the owner's decision, 2026-11-09,
[vocaro-engineering#305](https://github.com/vocaro/vocaro-engineering/issues/305)).
`light` once ran unadmitted where it started, and loads now as `heavy`, so a project may drop it
or the whole `resource` key. Arguments after `--`,
as in `qk run test -- --filter SUITE`, are appended to the lane's command. `--expect N` requires
exactly N executed tests: a run that selected those N and left some skipped or canceled exits 3,
unmeasured, and any other count fails. A session's environment does not travel to the second Mac,
so a test that needs a variable, such as a corpus path, runs there only in a lane whose command
sets it, as a gate's script does
([vocaro-engineering#285](https://github.com/vocaro/vocaro-engineering/issues/285)). Run any other
heavy command with `qk admit`:

```sh
python3 scripts/qk admit -- \
  xcodebuild test -scheme SCHEME -destination "platform=iOS Simulator,id=UDID"
```

Queuekeeper's own test runners are admitted the same way when nothing admitted them. `qk unittest`,
`qk xctest`, `qk simulator-tests` and `qk unity test` are what a lane's command
names, and inside a lane or `qk admit` they run in its admission. Started by themselves they once
took no slot and ran on this Mac beside whatever else ran there. Now such a runner is admitted and placed
first, as `qk admit -- COMMAND` would run it: `python3 scripts/qk unittest -s tests` waits for a
slot, on the second Mac first. In a folder with no `queuekeeper.json` it waits for a slot on this Mac.
`--unadmitted`, written after the runner's name, runs it with no admission on this Mac, which a test of
the runner itself needs:

```sh
python3 scripts/qk unittest --unadmitted -s tests test_core.ConfigurationTests
```

A command that is not Queuekeeper's, such as a bare `python3 -m unittest` or `swift test`, still takes
no slot unless it is run with `qk admit`.

Add `--model` for a generative-model run: on-device generation on this Mac or in a simulator,
which uses the host Mac's model, and other local generative models such as MLX on the GPU. Core
ML whose compute units include the GPU takes it too ([model admission](#model-admission)). A
lane says so with `"model": true` beside
`"resource": "heavy"`. Add `--exclusive` only for work that needs the host to itself: a
whole-machine or wall-clock measurement, a model run that is timed or compared with a baseline
among them ([long-running measurements](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#long-running-measurements)). Only the timed part of a
run is exclusive: build, install and set up first under `qk admit --here`, beside other sessions'
work, then admit the timed run alone with `qk admit --here --exclusive`, as a separate command, such
as `xcodebuild build-for-testing` and then `test-without-building`. Both take `--here`: each
`qk admit` chooses its Mac on its own, the second Mac first, so without it the build and the
timed run of what it built can land on different Macs (a `self` lane's parts stay together
without it). Setup, recovery and a functional
run whose seconds are not meant as figures are heavy work. An exclusive request inside heavy
admission is refused, so a lane whose run needs both is `self`, and its script admits each part,
as BreezEPUB's `book-opening` and PDFReflowLib's `memory-platform-calibration` do; a lane group can
be neither `self` nor run a `self` lane, since its one admission would hold the script's own.
queuekeeper runs a `self` lane unadmitted and trusts its script, so `qk doctor` warns of one whose command
neither calls `qk admit` in its own words nor runs a file of the checkout that does: such a lane
would run its work outside admission. The check reads text, so it catches a lane that admits
nothing, not one that admits some parts and runs others directly.
A gate suite is heavy work, never exclusive: one that runs the project's suites side by side says
`"wholeHost": true`, which keeps every other heavy job off its Mac while it runs
([heavy-work admission](#heavy-work-admission)). `qk admit --exclusive` and `qk remote admit
--exclusive` refuse a command that is word for word a heavy lane's `command` or `compileFirst`, and name the
lane: run it with `qk run LANE`, or give the lane `"resource": "exclusive"` if its run really needs the
host to itself. Any other command, and an exclusive or `self` lane's, is not refused. On 2026-11-12 a gate suite
sent as exclusive work held every slot of the second Mac, queued and then running, for 33 minutes, while every
other lane went to this Mac.
`--timeout` bounds the wait for admission. The command runs in the current directory with
its output on the terminal. Like a heavy lane, `qk admit` first refreshes the operating policy,
printing it to standard error, and records the command's receipt in
`.build/queuekeeper/admit/source.json`. It needs no `PYTHONPATH` and works from a session
worktree ([locating the toolkit](https://github.com/vocaro/vocaro-engineering/blob/main/guides/adoption.md#locating-the-toolkit-from-a-worktree)). A toolkit
clone older than `admit` reaches the same queue through the resource CLI:

```sh
PYTHONPATH="$QUEUEKEEPER_ROOT/src/python" python3 -m queuekeeper.resources -- COMMAND
```

Public project documentation may show plain commands for outside contributors on their own
machines; the admission rule is about this host. Project adapters (a `heavy_slot.py` shim,
scripts that import `queuekeeper.resources`) all call
`src/python/queuekeeper/resources.py`; there is no second queue to keep in step. A
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
([core-wait time](https://github.com/vocaro/vocaro-engineering/blob/main/guides/measurement.md#cpu-scheduling-and-core-wait-time)), and a ceiling that memory
pressure spoiled is unmeasured, not failed
([unmeasured ceilings](https://github.com/vocaro/vocaro-engineering/blob/main/guides/measurement.md#host-pressure-and-unmeasured-ceilings)). Such a gate is
heavy work like any other; one whose tests still assume a quiet host stays exclusive until they
are fixed. A test that a busy host decided reports itself unmeasured, and its lane exits 3
([honest outcome states](https://github.com/vocaro/vocaro-engineering/blob/main/guides/evidence.md#honest-outcome-states)). Admission counts a gate as one job
however many workers it starts, so bound its build and test parallelism
([parallel tests](https://github.com/vocaro/vocaro-engineering/blob/main/guides/parallel-tests.md#bound-what-runs-at-once)).

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

While no exclusive or whole-host request waits, HEAVY_SLOTS jobs (3) are always admitted. Beyond them one job
at a time is admitted, up to HEAVY_SLOTS_MAX (half the CPU count), while the CPU's busy share is
below HEAVY_BUSY_LIMIT (75, percent of the CPU's time), macOS reports normal memory pressure
(`kern.memorystatus_vm_pressure_level` is 1), and no slot was taken in the last
HEAVY_SETTLE_SECONDS (30) for the CPU to show. The busy share is the part of the CPU's time spent
outside idle over about the last 15 seconds, from the kernel's tick counters; every process on the
host keeps its readings in `cpu-samples.json` in the slot directory, so that one reading spans that
window without waiting for it, and a request's first five seconds of samples withhold the slot as
`settle` rather than judging it on less. It replaced the one-minute load average
([vocaro-engineering#200](https://github.com/vocaro/vocaro-engineering/issues/200)): on a Mac
running simulators the load counts their many daemons' brief wake-ups, and on 2026-11-06 the second
Mac read a load of 27 to 66 against a limit of 7.5 while its CPU was up to half idle; from 2026-11-05
its load was under the limit only 26% of the time, about when nothing heavy ran, and 36 of 300
admissions there took a slot beyond the guaranteed three. The load is still logged beside the busy
share for audits. Setting HEAVY_SLOTS_MAX to HEAVY_SLOTS
restores a fixed limit. Admission cannot preempt: a job admitted while the host was quiet may
become heavy later. Exclusive work holds every slot up to the maximum. A whole-host lane, a gate
that runs the project's suites side by side (`"wholeHost": true` on the lane or its group), takes
every guaranteed slot instead of one, and waits while any of them is busy (`a whole-host lane needs
every guaranteed slot (HEAVY_SLOTS=3) free, and 1 is busy`), so that two such lanes never run on
one Mac at once, and while it runs no other heavy work is admitted beside it, not even in a slot
beyond the guaranteed ones (`it waits behind a whole-host lane holding the host: PDFReflowLib
gate`): a gate's load dips between its phases, and on 2026-11-05 MathMLLayout's gate squeezed in
beside PDFReflowLib's on such a dip, whose memory pressure then left that gate unmeasured after
1,116 s (the owner's decision, 2026-11-05,
[vocaro-engineering#196](https://github.com/vocaro/vocaro-engineering/issues/196)). queuekeeper sends
each lane to the Mac that has the slots it needs ([which Mac runs a lane](#which-mac-runs-a-lane)).
Nested work reuses its inherited admission, whole-host or not.

Whole-host and exclusive differ in when they start and whom they make wait. A whole-host lane
starts once the guaranteed slots are free, beside any lighter work already running beyond them,
and from then on no other heavy work is admitted until it ends. While it waits, it keeps the
guaranteed slots for itself, and a heavy request that arrives meanwhile takes only a slot beyond
them, as the queue paragraphs below say. It is for a gate, whose time is measured with the Mac to
itself but whose wait must not stall every other session. Exclusive work holds every slot up to
the maximum, is admitted one request at a time in arrival order once the work running before it
has ended, and holds every later request back until it ends. It is for a timed measurement that
needs a quiet host and nothing else. A whole-host lane cannot be exclusive,
since exclusive admission holds the host already. In the choice of Mac the two count alike: a Mac
running either has no spare capacity for any heavy lane, which goes to the other Mac first and is
offered to both, to share where admission allows, only when neither has capacity
([which Mac runs a lane](#which-mac-runs-a-lane)).

A host's own limits go in its limits file, `admission-limits.json` beside the admission log
(`~/Library/Application Support/queuekeeper/`; an isolated HEAVY_SLOT_DIR namespace keeps
its own there), which every process on that host reads. Its keys are `slots`, `slotsMax`,
`busyLimit` and `settleSeconds`, for the four limits above; each is a number, or
`{"perCore": F}` for F times the CPU count, which for a slot count is rounded down and at least
1. A key it leaves out keeps its default; without the file every default stands. For example,
`{"slots": 4, "busyLimit": 85}` guarantees four slots and opens extra ones while the CPU is under
85% busy; `busyLimit` is a percentage, and a share of the cores means nothing for it. The former
`loadLimit` is now an unknown key. A file admission cannot read, with an unknown key or a value of
the wrong kind, refuses every request on that host with an error naming the file: its owner
fixes it rather than queuekeeper guessing the limits. Since 2026-11-07 the owner wants the second Mac
loaded as much as possible and this Mac a little less, so the second Mac's file sets the higher
`slotsMax` and `busyLimit`; the measurement #49 describes may still revise them. The files live
on each Mac, outside the repository, so this guide does not copy their values:
`qk admissions usage` shows both Macs' limits as they stand, and `qk admissions limits` one
Mac's with where each comes from.

To load a Mac more, raise its `slotsMax` and `busyLimit`, not its `slots`. A whole-host lane needs
every guaranteed slot of a Mac free before it starts there, so each guaranteed slot added makes
that Mac a worse place for gates: on 2026-11-07 four guaranteed slots on the second Mac sent
gates to this Mac instead, and the second Mac went back to three. Raise `slots` only when fewer
gates on that Mac is acceptable.

So a key a toolkit adds to the file is a breaking change for every copy older than it: such a copy
refuses all its heavy work on that host as misconfigured. Write a new key to a host's file only once
nothing on that host runs an older copy: work started before the key landed has ended, no
`QUEUEKEEPER_ROOT` names an older copy, and no session worktree of the toolkit that runs its
own code is behind the landing. The shared checkouts move to `origin/main` by themselves. `busyLimit`,
which replaced `loadLimit` on 2026-11-06 (vocaro-engineering#200), was the first such key.

A limit's environment variable overrides the file for the process it is set in. Each process
reads its own environment, so set one for every session or for none: an exclusive job holds only
the slots below its own maximum, and a caller with a higher one can take slots above it.
`python3 scripts/qk admissions limits` prints the limits a process started like it would use,
and where each comes from. Waiting messages name a limit by its variable whatever set it.

All requests record their arrival in `exclusive-queue.json` in the slot directory. An older
ordinary request runs before a later exclusive or whole-host request; ordinary requests can still
share slots with each other. An older exclusive request holds later requests back while the slots
it needs drain. A whole-host request similarly keeps the guaranteed slots for itself. Jobs already
running finish normally. Each request still needs its own resources: admission order does not
create a free checkout, unlock a screen, or release a model lock.

`qk run` and `qk admit` retain one place across local waits between remote offers, for every
request kind. A remote dispatch carries elapsed request age, which the receiving Mac translates
to its own clock. A declined offer releases its checkout and queue record; a later offer restores
the original age when a checkout is available. After the first reply, retries carry the same
receiving-host timestamp unchanged, so connection latency does not shift it on each offer.
Capacity preflight ignores exclusive requests that
arrived after the requesting work. This keeps later exclusive arrivals from repeatedly overtaking
ordinary work. Queue age establishes order, not an absolute start deadline: unavailable resources
can still prevent admission. The transport age stops at the receiving CLI step: leaf commands
keep inherited admission tokens, but independently admitted child work starts with a fresh age.
This also applies to each independently admitted part of a self lane.

A queued request holds no slots. Admission drops it when its process has ended or its PID has a
different start time. A stop removes its record; a timed-out local attempt can retain its place
until the overall caller finishes waiting, when the caller removes it. A retained place stops
holding later work back once it has not asked within HEAVY_PLACE_SECONDS (180), while keeping
its arrival for its next attempt. This freshness bound also applies to exclusive requests.

A whole-host request queued ahead holds back less. Exclusive and whole-host requests behind it
wait their turn, but other heavy work goes on in a slot beyond the guaranteed ones while the host
shows spare capacity, and takes none of the guaranteed slots, so that they drain toward the
whole-host lane: `heavy: waiting (0s): a guaranteed slot is free but kept for a whole-host lane
queued ahead of it: EPUBLib gate; and …`. Until 2026-11-05, when the owner chose this on #174's evidence, a waiting whole-host lane held nothing,
and heavy requests that arrived meanwhile took each guaranteed slot as it was freed, so on a Mac
where sessions started heavy lanes one after another the guaranteed slots need never all be free
and the gate could wait out its 5,400 s
([vocaro-engineering#174](https://github.com/vocaro/vocaro-engineering/issues/174)). Where
HEAVY_SLOTS_MAX equals HEAVY_SLOTS, or the host shows no spare capacity, the heavy work waits
for the whole-host lane as it would for exclusive work. A whole-host request holds anything back
only while its process keeps asking: one that last asked more than HEAVY_PLACE_SECONDS (180) ago
holds nothing back, though it keeps its place. That is how `qk run` keeps a lane's
place across its waits on this Mac while it also offers the lane to the second Mac
([which Mac runs a lane](#which-mac-runs-a-lane)): the place stays between waits, so the lane
does not rejoin behind requests that came meanwhile, and it holds nothing back once the offer has
kept it away that long, as when the second Mac took the lane and runs it. It leaves the place once
it no longer waits here, admitted or not. How long an offer takes, and so whether 180 s covers
every gap between two waits, is unmeasured.

Nested work reuses its inherited admission. Admission exports `QUEUEKEEPER_RESOURCE_TOKEN` and keeps
its lock descriptors open across exec. A nested `qk run`, resource CLI call or project gate
validates the inherited descriptors against their lock files, the owner's nonce, its guard
entry and its process start time, then runs inside the outer admission without taking a slot,
and without waiting behind a queued exclusive request, which waits for the outer job to end.
Nesting the queue therefore never obtains a second slot, and an exclusive request inside shared
admission is refused, as is a model request inside admission that does not hold the model. Work
that needs the whole machine asks for exclusive admission.

The toolkit's process supervisor passes a command only those of the token's descriptors that its
own process holds, each open on the lock file the token names, which names the owner.
`qk unittest` passes each module's process the same descriptors and no others, so a nested
admission in a test reuses the run's admission, as it does under `python3 -m unittest discover`.
A process started without them, as a plain `subprocess` call starts its child, keeps the token but
not the admission. A nested admission there is refused rather than given a slot, and the
supervisor and `qk unittest` pass on none of the numbers the token names: there each is closed,
which would stop the command from starting with EBADF, or open on another file, which the command
would get.

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
| `slots` | `all 3 guaranteed slots are busy and so is every slot up to HEAVY_SLOTS_MAX=9`, or `… and HEAVY_SLOTS_MAX=3 allows none beyond them`; for a whole-host lane, `a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=3) free, and 1 is busy`; for exclusive work, `exclusive work needs every slot up to HEAVY_SLOTS_MAX=9 free, and 2 are busy` |
| `settle` | `all 3 guaranteed slots are busy and the CPU does not yet show the job admitted 12s ago, within HEAVY_SETTLE_SECONDS=30`, or `… and the CPU has been read over 2s, short of the 5s a reading spans` |
| `cpu` | `all 3 guaranteed slots are busy and the CPU is 91% busy, at or above HEAVY_BUSY_LIMIT=75%`, or `… the CPU's busy share, which HEAVY_BUSY_LIMIT=75% bounds, is unavailable`; a toolkit older than vocaro-engineering#200 logged `load` for the load average |
| `memory` | `all 3 guaranteed slots are busy and memory pressure is level 2 (warning), not 1 (normal)`, or `… memory pressure, which must be level 1 (normal), cannot be read` |
| `model` | `the on-device model, one run at a time, is held by LABEL` |
| `exclusive` | `it waits behind exclusive work queued ahead of it: LABEL`, or `… holding the host: LABEL` |
| `wholeHost` | `it waits behind a whole-host lane queued ahead of it: LABEL`, for exclusive and whole-host requests, and `it waits behind a whole-host lane holding the host: LABEL` for other heavy work while one runs; for other heavy work behind a queued one, `a guaranteed slot is free but kept for a whole-host lane queued ahead of it: LABEL; and HEAVY_SLOTS_MAX=3 allows none beyond them`, or `… and every slot beyond them up to HEAVY_SLOTS_MAX=9 is busy`, or the `settle`, `cpu` or `memory` reason. With every guaranteed slot busy anyway, the reason is the `slots` or other kind's, as with nothing queued |
| `stranded` | another reason above, then `; the guard entry of LABEL, PID 4242, is dead and requires inspection (it has no process record), and is counted as slot 1; …`, `uncertain` for an entry marked so, with why [recovery](#lock-reclamation) left it in parentheses (`its owner still runs`, `it has no process record`, `its process record cannot vouch for every command` or `2 of its processes still run: PID 4250, 4251`) and what it is counted as (`slots 0, 1, 2 and the model` for exclusive work); a model request's reason is `the on-device model, one run at a time, is counted as held` |
| `guard` | `the guard PATH is unreadable or not in admission's format and requires inspection; …` |

In both rows `…` names `python3 scripts/qk admissions recover`.

Each kind is the one the [admission log](#admission-log) records for the refusal. After its
timeout (5,400 s by default) admission fails without reclaiming any lock, naming the same reason,
the Mac, the time it waited and that nothing ran, and an exclusive or whole-host request leaves the
queue, unless it keeps its place across waits. Behind work known to be running it says
`Resource queue busy on MAC: REASON; held by LABEL (slot0, slot1); waited Ns; nothing ran`, which
is an ordinary wait. It says `Resource queue busy or an orphan requires inspection (REASON); no lock was reclaimed; …; waited Ns on MAC; nothing ran` only for what no
running admission vouches for: a `guard` or `stranded` reason, or a slot or the model that is busy
while its file names no running admission. When neither Mac admits the work, `qk run` and
`qk admit` give the second Mac's reason and this Mac's last wait
([vocaro-engineering#270](https://github.com/vocaro/vocaro-engineering/issues/270)). A script that runs several jobs at once leaves at least one of the guaranteed
HEAVY_SLOTS for other sessions.

Admission recovers each dead or uncertain entry it meets, as `qk admissions recover OWNER`
would: it removes one once its owner has ended and a census against its process record finds
nothing it ran still running ([lock reclamation](#lock-reclamation)). An entry recovery leaves,
stranded, is never removed: it counts as the slots it held until it is recovered, so that only
work beyond those slots waits. A stranded entry of exclusive work counts as every slot up to
HEAVY_SLOTS_MAX, so all heavy work waits behind it; any other counts as the slots whose files
name its identity, as each admission writes them, or, where none does, as the lowest slot not
already counted; one that held the model counts as its holder. Exclusive work waits behind any
stranded entry, since it needs every slot. A queued exclusive request holds back no work while a
stranded entry is counted, since it cannot be admitted until the entry is recovered, and a queued
whole-host request none while one is counted as a guaranteed slot; once it is recovered, the
queue's order applies again. Work admitted beside a stranded entry prints `heavy:
admitted beside a stranded entry: …` with the entry, its slots and the recover command, and its
log line names them ([admission log](#admission-log)). Admission checks the entry again every
HEAVY_RECOVER_SECONDS (60). `python3 scripts/qk admissions recover` lists those entries.
This repository's `.claude/settings.json` allows agents to run it, with or without an owner, so
that one can clear such an entry once its processes end; the command refuses any entry that is
not provably finished (owner's decision, 2026-11-02). Other projects' settings do not carry the
rule, and need not: every project's heavy work is admitted by this toolkit, whose admission
recovers each provably finished entry itself, an owner killed before its first command among them,
at its next attempt and every HEAVY_RECOVER_SECONDS after, so no session has to run the command to
free one (vocaro-engineering#315). What recovery leaves needs a person's look, and `qk queue
status` says when a gate is stuck behind it ([the merge queue](maintenance.md#the-merge-queue)).
An entry it leaves because nothing proves it finished is removed by hand with `qk admissions
remove`, which no allow rule matches ([lock reclamation](#lock-reclamation)).

The rule: a disappeared owner's children may still be running, and they keep what its entry
held, its slots and the model if it held it, and nothing more. The rest of the host stays open
to other sessions' work. Until 2026-11-01 the rule was that those children might still use the
whole host, so a stranded entry refused every session's heavy work on it until it was recovered
([vocaro-engineering#89](https://github.com/vocaro/vocaro-engineering/issues/89)).

A request's label names it in waiting messages, the guard and the [admission log](#admission-log).
A lane run's label is its project's name, from queuekeeper.json, a space and the lane, such as
`EPUBLib gate`, so that the `gate` lanes of several projects, or the `tests` lanes of
BackgroundTaskLib and FileHandoffLib, stay apart. A request waiting behind EPUBLib's gate prints
`heavy: waiting (0s): it waits behind exclusive work queued ahead of it: EPUBLib gate`. An
`qk admit` request's label is its command line, and a caller that admits its own work names it,
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
limit of the hardware ([long-running measurements](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#long-running-measurements)), and exclusive
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
([embedding correctness](https://github.com/vocaro/vocaro-engineering/blob/main/knowledge/ml/embedding-correctness.md)). A model run that is timed or
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

The log is `~/Library/Application Support/queuekeeper/admissions.jsonl`, in the account's
durable state, because an audit looks back weeks and files in the slot directory are the
temporary-file cleaner's after three idle days
([scratch and result directories](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#scratch-and-result-directories)). A HEAVY_SLOT_DIR namespace
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
  `qk admit`, or the label a caller gives, such as `warm push REV`.
- `slots`: the slot numbers held, on `admitted` and `ended` lines.
- `waited`: seconds from the request to its admission or withdrawal, on `admitted` and `withdrawn`
  lines; `held`: seconds from admission to its end, on `ended` lines.
- `holders`, `load` and `busy`, on `admitted` and `withdrawn` lines: how many admissions the guard
  recorded when the request asked, a dead or uncertain one among them, the one-minute load average
  then and the CPU's busy share, null until a reading spans five seconds.
  A request admitted at once records nothing more.
- `wait`, on the `admitted` or `withdrawn` line of a request that an attempt refused: why it waited.
  `refusals` counts its refused attempts of each kind, about one a second; `first` and `last` are
  the kinds of its first and last refusals; `load` holds the one-minute load its last attempt read,
  as `last`, and the highest any read, as `max`, and `busy` the same of the CPU's busy share; and
  `memory` is the highest memory pressure level read beside its refusals, 1 normal, 2 warning and
  4 critical. A load, busy share or level that could not be
  read is null. A refusal's kind is the check that refused it:
  - `slots`: for heavy or model work, every slot up to HEAVY_SLOTS_MAX is busy, which is every
    guaranteed one when no adaptive slots are configured; for exclusive work, any slot is;
  - `settle`, `cpu` or `memory`: every guaranteed slot is busy, and a slot taken less than
    HEAVY_SETTLE_SECONDS ago or CPU samples spanning less than five seconds, a busy share at or
    above HEAVY_BUSY_LIMIT or one that cannot be read, or memory pressure that is not normal
    withholds another (`load`, in a line an older toolkit wrote, was the load average);
  - `model`: another run holds the on-device model;
  - `exclusive`: an exclusive request is queued ahead, or exclusive work holds the host, which
    refuses the request at its slots or the model;
  - `wholeHost`: a whole-host request is queued ahead of an exclusive or whole-host one, or keeps a
    free guaranteed slot from other heavy work, which found no slot beyond them
    ([vocaro-engineering#174](https://github.com/vocaro/vocaro-engineering/issues/174));
  - `stranded`: the request was refused at its slots or the model while a stranded entry, dead or
    uncertain and left by recovery, was counted as some of them;
  - `guard`: the guard is unreadable or not in admission's format, and requires inspection.
    Until 2026-11-01 this kind also counted a stranded entry, which refused every request.
- `stranded`, on the `admitted` or `withdrawn` line of a request whose latest attempt counted a
  stranded entry: each such entry's `identity`, `pid`, `label`, `uncertain`, `why` recovery left
  it, as the waiting message gives it, the `slots` it is counted as and whether the `model`.
- `outcome`, on an `ended` line: `released` when the command finished, whatever its exit status;
  `not-started`; `stopped` or `timed-out` when a stop or the command's timeout saw it and every
  observed descendant end; or `uncertain` when the guard entry was kept for inspection
  ([lock reclamation](#lock-reclamation)). On a `withdrawn` line: `timed-out`, which includes
  `qk run --if-free` declining; `stopped`; or `failed`.
- On a `recovered` line, which `qk admissions recover`, `qk admissions remove` or admission
  itself writes when it removes an entry: `pid` is the entry's owner; `uncertain`, whether the
  entry was marked so; `commands`, `processes` and `spaces`, how many unsettled commands, recorded
  processes and process groups or sessions its census checked, all 0 for a removal by hand;
  `automatic`, true when admission recovered it, absent when a person did; and, for a removal by
  hand ([lock reclamation](#lock-reclamation)), `byHand` true, `unproven`, why recovery left the
  entry (`it has no process record` or `its process record cannot vouch for every command`), and
  `why`, the person's reason as `--why` gave it.

An `admitted` line with no `ended` line is still held, or SIGKILL ended its admitting process,
which leaves its guard entry for recovery as well, and a `recovered` line follows once it is
recovered. A log that cannot be written never fails or holds up admission or release: admission
prints `heavy: the admission log was not written: ERROR` and goes on.

A request's `wait` says why it waited, which an audit could only infer from the holders logged
beside it. Whether admission's limits hold back work the host could run turns on the kinds: a wait
behind `slots` is the guaranteed and maximum counts, and one behind `cpu`, `memory` or `settle`
the signals that open the slots between, at the busy share and load its line records
([vocaro-engineering#49](https://github.com/vocaro/vocaro-engineering/issues/49)). A line an older
toolkit wrote has no `holders`, `load` or `wait`, and is read as it is. One may also count `legacy`
refusals, from when admission waited while a gate held StudyWright's former host lock
([vocaro-engineering#25](https://github.com/vocaro/vocaro-engineering/issues/25)); they are read
and counted as any other kind.

`qk admissions` summarizes the log over a period:

```sh
python3 scripts/qk admissions --since 2026-10-20 --until 2026-10-27
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
waited read; `CPU busy when asked mean 48%, max 97%, while waiting max 100%` gives the same of the
busy share. A request counts in the period in which it was admitted or withdrawn. A date or time
without a zone is local. A period that begins before the log's first line is reported as such:
older requests have rotated out and count nowhere. Lines that cannot be read, such as one a full
volume cut short, are counted and skipped. Two projects' lanes of one name are counted apart, as
`EPUBLib gate` and `StaticSVG gate`; lines an older toolkit wrote count under the lane alone, as
`gate`. To summarize the second Mac's log, pass a copy, the previous generation first:

```sh
python3 scripts/qk admissions <(ssh build-host \
  'cd "Library/Application Support/queuekeeper" && cat admissions.1.jsonl admissions.jsonl 2>/dev/null')
```

### Who holds and who waits

`python3 scripts/qk admissions now` shows admission as it stands on this Mac and the second Mac:
each holder's slots, kind and hold time, and each waiter's wait and the reason it prints itself,
with where each one's work comes from: its repository, branch, worktree, agent session and Mac
(`resources.work_origin`), the sender's for work the second Mac runs for this one. It changes nothing. A Mac it cannot read in 10 seconds is unknown, never idle, and this Mac's reading
stands beside it. `--json` gives the same; `--here` reads only this Mac, and is required under an
isolated `HEAVY_SLOT_DIR` or `QUEUEKEEPER_GATE_LOCK_FILE`. What it reads, and what it marks partial, is in
`offload_host.admissions_now`; each waiter's record is `resources.Admission._observe`
([#342](https://github.com/vocaro/vocaro-engineering/issues/342)). It checks the processes named with one
`ps`; when that fails or takes 5 seconds, it asks the kernel PID by PID, which reads no process's arguments, so
that one bad PID or one failed `ps` does not leave every holder unchecked. A holder whose PID is gone, or runs
another process, is ended and shown stranded. One whose PID runs but whose start neither can read is shown as
could not be checked, not as ended: a holder alive but slow to confirm is not freed on a guess. Recovery reads
an owner's end the same way ([#23](https://github.com/vocaro/queuekeeper/issues/23)). It does not yet flag a wait as
too long: the owner deferred that until there is history to calibrate it.

Under each running holder, a `progress:` line gives each of its leaves' live quiet-interval
observations: the lane and Mac, how long it has been quiet against the bound it declares, and
whether that bound is passed, when it last made progress, its longest quiet and its samples
([observe a quiet lane](parallel-tests.md#observe-a-quiet-lane)). Only a lane that declares
`stall` publishes one, so a holder whose leaves publish none says it is not observed, and one
whose processes cannot be checked says its progress is unknown; neither is read as idle or as
busy. A passed bound is evidence, not a verdict: it ends nothing that has not declared
`"action": "stop"` ([vocaro/queuekeeper#70](https://github.com/vocaro/queuekeeper/issues/70)).

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

Both Macs are for work. Heavy work goes to whichever Mac has spare capacity for it now, the
second Mac first: this Mac takes what has to run here, what the second Mac cannot run, and heavy
work while the second Mac has no spare capacity and this Mac has, and while neither has, the work
goes to whichever admits it first. When one Mac is busy and the other idle, work starts on the idle
one, and a session never asks the owner which Mac to use (the owner's decision, 2026-11-04,
[vocaro-engineering#168](https://github.com/vocaro/vocaro-engineering/issues/168)). This Mac is the
owner's workstation, busy with work the queue never sees, while the second Mac is otherwise idle
and has more memory and disk; its screen stays the owner's, which is a separate rule
([attended Mac sessions](#attended-mac-sessions)). queuekeeper, not the session, chooses the Mac;
configuration calls sending work there offloading it. The second Mac runs work under its own heavy-work
admission: each host's queue sees that host's load, so the second Mac is no private slot pool.
`qk remote` reaches it by the SSH alias `build-host`, or `second-mac`; `--host` or
`QUEUEKEEPER_OFFLOAD_HOST` names another destination. Until 2026-10-28 the second Mac was the build
host. Records made until then, measurements among them, keep that name, and queuekeeper refuses the
configuration's earlier names ([earlier names](#earlier-names)).

```sh
python3 scripts/qk run LANE [-- ARGUMENTS]            # a heavy lane: whichever Mac has spare capacity, the second Mac first
python3 scripts/qk run --here LANE                    # this Mac
python3 scripts/qk run --offload-only LANE            # the second Mac if it can start it now, never here
python3 scripts/qk admit -- COMMAND                   # any other heavy command: the second Mac first, as a lane goes
python3 scripts/qk admit --here -- COMMAND            # this Mac: a command whose products the session uses here
python3 scripts/qk remote run LANE [-- ARGUMENTS]     # any lane, on the second Mac
python3 scripts/qk remote admit [--exclusive | --model] -- COMMAND
python3 scripts/qk remote passed LANE [--commit REV]
python3 scripts/qk remote setup [FORMULA ...] [--check]  # the gates' tools (Setting up the second Mac)
python3 scripts/qk remote setup --runtimes [--check]     # both Macs' simulator runtimes, evened out
```

### Which Mac runs a lane

Every lane in queuekeeper.json, a short check as much as a gate, goes to the second Mac first
unless it says why it stays on this Mac, as `"here": "REASON"`, which configuration validation
requires to be a nonempty reason, and runs on this Mac when the second Mac is at capacity (the
owner's decision, 2026-11-07). A lane says `here` when it needs a physical device, or a measurement
tied to this Mac's hardware, such as goldens rendered on this Mac's GPU. A `self` lane runs where
it starts, and its script's `qk admit` commands run there: its parts share build products, a build
and then an exclusive timed run of what was built, so `qk run` sets `QUEUEKEEPER_SELF_LANE` to the
lane's name in its command's environment, and each `qk admit` under it stays on that Mac, its
receipt's `hereBecause` saying `a part of the self lane LANE, …`.
A foreground lane, which takes the screen, goes to the second Mac and only there, where queuekeeper holds
the second Mac's foreground for it ([second Mac foreground](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md)). Simulator
lanes and background Mac UI lanes go to the second Mac like the rest, and a lane's first run there is its
check (the owner's decision, 2026-10-28): counts that differ from this Mac's are a defect to fix,
not a reason to keep the lane here. A lane group runs its lanes under its one admission on one
Mac, so a group stays here when it, or one of its lanes, says `here`. `"buildHost"`, which once
marked a lane for the second Mac, is refused, true or false ([earlier names](#earlier-names)).

#### Build Macs for merge queue gates

To add Macs, give each a distinct SSH alias and follow [the build host setup](#setting-up-the-second-mac).
On the Mac that starts the queue workers, put the existing build Mac and the new ones in
`~/Library/Application Support/queuekeeper/gate-hosts`, in the order the queue should try
them. The file holds a comma-separated list of aliases.

```text
build-host,gate-03,gate-04
```

Workers started from Terminal, Codex, or the signed LaunchAgent read the same file for that account.
When the file is missing, an existing `QUEUEKEEPER_GATE_HOSTS` export still works. With neither setting,
the queue uses this Mac and the existing offload host. An explicit `HEAVY_SLOT_DIR` namespace reads
its own `gate-hosts` file from that directory.

The queue includes this Mac as the local fallback automatically. Each alias must identify a different physical Mac.
For each eligible lane, the queue tries the first listed Mac with capacity. If that Mac is
unreachable or declines before the work starts, the queue tries the next alias. When all decline,
the ordinary gate can take local admission and rechecks the build Macs while waiting. A
misconfigured host stops the offer with its diagnosis, and an interrupted connection to a run
that may have started yields no verdict.

The list applies to merge queue gates. Foreground lanes use the designated foreground Mac, and
standalone `qk run` and commands that act on one remote, such as `qk remote setup`, use the
ordinary offload host or their explicit host. `QUEUEKEEPER_OFFLOAD_HOST=none` disables offloading,
including the queue list.

#### Check gate placement without running a gate

Run `python3 scripts/qk queue placement` from the project. It reads the first gate lane on
`origin/main`, lists the build Macs in their configured order, and checks each host's current
capacity over SSH. Give a lane name to inspect a different lane, or add `--json` to save the
timestamped readings. This command leaves queue entries, workers, and admissions alone.

Read the refusal beside the slot counts. With one of three guaranteed slots occupied, a
`wholeHost` gate waits for the last occupied guaranteed slot. A queued exclusive request also
holds back later work while existing admissions drain. When that request starts, it can take
all slots up to the host's maximum. The same PID and process start in two readings identify the
same blocker. The admission identity lets you match a running holder to its release in that host's
admission log. Keep shell commands and project evidence with the owning project.

Use `qk queue status` for a gate already waiting and `qk admissions usage` for capacity and
utilization history on this Mac and the ordinary offload host. A placement reading is a snapshot.
The host checks admission again when the lane actually starts.

[Cloud gate runners](cloud-gate-runners.md) compares overflow hosts and the cost of a pilot.

#### Screen state

No guide, policy or log line assumes either Mac's screen state. queuekeeper reads it on the Mac that runs
a lane, with the check `qk host screen [--remote]` runs
([checking a Mac's screen](https://github.com/vocaro/vocaro-engineering/blob/main/guides/background-mac-testing.md#checking-a-macs-screen)), and writes what it
read, or `unknown` and why, never a guess (vocaro-engineering#185). The owner keeps the second Mac
unlocked; both Macs can lock, by hand or by idle time under their settings.

A command started over SSH on the second Mac runs in an audit session of its own and is not
trusted for accessibility (on the 27.2 beta 26B5091g as on 27.2 26B123), yet the window server
gives it the console user's session: it opens offscreen windows and reads its own accessibility.
What a locked screen or a sleeping display has been seen to stop, on which Mac and build, what
wakes a locked screen's display and keeps it on, and what is unmeasured, is dated history in
[lock screen and display sleep](https://github.com/vocaro/vocaro-engineering/blob/main/guides/background-mac-testing.md#lock-screen-and-display-sleep): on macOS
27.2 (26B123), locked for those measurements, EPUBLib's Mac suite and whole gate passed on the
second Mac (2026-10-28 and 10-29), while the accessibility API (`AXUIElement`) vended no offscreen
window behind the lock, as on this Mac on 27.0. A run whose mechanism a lock or a sleeping display
stops is unmeasured, never passed ([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/agents/core.md#shared-resources)).

A lane whose tests need an unlocked console screen, such as one that reads its own tree through the
accessibility API, says `"unlockedScreen": true`; a group that runs such a lane says so too. queuekeeper
reads both Macs' screens with their capacity and places the lane only on a Mac whose screen reads
unlocked, `locked` and `unknown` alike counting as no room for it, and its placement line says so:
`the lane needs an unlocked screen, and the second Mac's is locked (screen: locked (...) on NAME at
HH:MM:SS)`. The second Mac declines such a lane before it takes a checkout while its screen is not
unlocked, and this Mac does not admit it while its own is not, so when neither Mac reads unlocked
the lane waits, offered to the second Mac again as any waiting lane is, instead of running
unmeasured and using up the merge queue's no-verdict runs. A lane that does not declare it is placed
as before, whatever either screen reads. `"display": true` is no substitute: it wakes the display and
keeps it on, behind a lock too, but unlocks nothing, so a `"display": true` lane whose tests report a
locked screen as unmeasured also says `"unlockedScreen": true`, and so does each group that runs it.
Without it, on 2026-11-07, the merge queue sent EPUBLib's gate to a locked Mac three times in a row,
each run unmeasured, until the queue held
([vocaro-engineering#293](https://github.com/vocaro/vocaro-engineering/issues/293)). `qk doctor`
warns of a `"display": true` lane that leaves `unlockedScreen` out; one whose tests measure behind a
lock says `"unlockedScreen": false`. Pagepour's gate and Mac suites declare it: its Mac lanes,
`mac-tests` and `mac-purchases`, read their tree through the accessibility API until their tests
move to the in-process NSAccessibility methods
([Pagepour#123](https://github.com/vocaro/Pagepour/issues/123)).

Once admitted, a lane that says `"unlockedScreen": true` or `"display": true` has the screen of the
Mac that runs it read and logged, `queuekeeper: screen: STATE (...) on this Mac (NAME) at HH:MM:SS`, or on
the second Mac, and kept in its receipt as `screen`, and read again, as `screenAfter`, when it ends
unmeasured, after a line naming that Mac: `queuekeeper: LANE unmeasured on this Mac (NAME)`. Any lane that
ends unmeasured with a log saying the screen is locked is read again the same way. A lane that runs on
this Mac after it was offered to the second Mac says so in one line, with why, before it starts:
`queuekeeper: gate runs on this Mac (NAME), not the second Mac: REASON; the second Mac's lines above are from
the offer it did not take`. The merge queue's hold for runs that gave no verdict names the Mac the
last ran on, and quotes the last screen line of its log. A run whose last reading shows a locked screen
does not count toward that hold, and the queue runs the lane next with `qk run --unlocked-screen`,
which places it as `"unlockedScreen": true` does, on an undeclared lane too
([maintenance](maintenance.md#the-merge-queue), vocaro-engineering#293).

`qk run` sends the lane to the Mac that has spare capacity for it, whole or filtered
(`qk run LANE -- FILTER`), over the working tree as it is: a tree with uncommitted changes or new
files goes as a snapshot commit ([how a run reaches the second Mac](#how-a-run-reaches-the-second-mac)),
so a session's iteration over its edits runs there too. Both Macs are for work, the second Mac
first, and the choice is queuekeeper's, by what each Mac's admission would admit, never the session's or
the owner's (the owner's decisions, 2026-10-28 and 2026-11-04,
[vocaro-engineering#168](https://github.com/vocaro/vocaro-engineering/issues/168)). Before it
offers the lane, `qk run` reads both Macs' capacity for it: a Mac has spare capacity while a
guaranteed slot is free there, every guaranteed slot for a whole-host lane and every slot up to
HEAVY_SLOTS_MAX for an exclusive one, its CPU's busy share is under its own HEAVY_BUSY_LIMIT, and
no whole-host lane or exclusive work runs there. On 2026-11-04 the second Mac, 10 cores, ran two
EPUBLib gates side by side at a load of 719 while this Mac, 18 cores, idled at 3, since its three
guaranteed slots admitted the second gate whatever the load, and 8 of 12 gates there that day
ended with no verdict; the rule above is what that cost.

"The second Mac first" orders the offer; it does not keep work off this Mac. Some work is never
offered: `qk admit --here`, `self` lanes and the `qk admit` parts their scripts run, `--here` runs,
foreground work and lanes that say why they stay here run where they start, and the receipt's
`hereBecause` says so. Light lanes and `qk admit` without `--verify` were never offered until
2026-11-07; they are now, as every lane is (the owner's decision, 2026-11-07). Offered work fills
the second Mac's guaranteed slots first, so a whole-host lane, such as a gate, which needs all of
them free, seldom finds them free there and starts on this Mac instead. On 2026-11-06 this
Mac's fan ran while the second Mac's stayed quiet, yet the second Mac was 97% busy and had run
three or four heavy jobs for 48% of the previous three hours, while this Mac ran one, a whole-host
gate, for 84% of them; it took hand scripting to show it. Judge a Mac's use by its reading, not by
its fan: `qk admissions usage` shows both Macs' capacity now side by side, with whether each has
room for a heavy and for a whole-host lane, the share of the last 3 hours (`--since` and `--until`
set another period) each Mac spent at each count of admitted heavy jobs, and this Mac's admissions
by why they ran here: never offered (`qk admit --here`, a self lane's part, foreground work,
`qk run --here`, a lane that says `here`, nested work, and in lines from before 2026-11-07
`qk admit` without `--verify`) or offered and declined, with the second Mac's reason. Admission lines
record their placement from 2026-11-07 on, and older ones count as unrecorded under their labels.
A `self` lane takes no admission of its own and is not counted; the parts its script admits are.

The reading applies admission's rule for a whole-host request queued there
([heavy-work admission](#heavy-work-admission)): while one whose process runs has asked within
HEAVY_PLACE_SECONDS, the Mac keeps its guaranteed slots for it, so a whole-host or exclusive lane
has no spare capacity there (`the guaranteed slots of the second Mac are kept for a whole-host
lane queued ahead: LABEL, which the lane would wait behind`), and a lighter heavy lane has it only
in a free slot beyond them (`…: LABEL, and every slot beyond them up to HEAVY_SLOTS_MAX=5 is
busy`), and only as admission takes such a slot: once HEAVY_SETTLE_SECONDS have passed since the
Mac's last admission (`…: LABEL, and the CPU of the second Mac does not yet show the job admitted
12s ago, within HEAVY_SETTLE_SECONDS=30`), under its HEAVY_BUSY_LIMIT, and while its memory
pressure is level 1, normal, and can be read (`…: LABEL, and on the second Mac memory pressure is
level 2 (warning), not 1 (normal)`). The reading reports the seconds since that admission, the
Mac's own HEAVY_SETTLE_SECONDS and its memory pressure level for this, read from the same limits
as its admission (`qk admissions limits`); a reading from a toolkit older than
vocaro-engineering#201 has none of them and withholds nothing by them. A free guaranteed slot
needs neither, here as in admission. Until vocaro-engineering#201 the reading judged that slot by
the load alone, and queuekeeper could send a lane to wait on a Mac whose admission then withheld it. A
queued request whose process has ended, one that has not asked lately, one behind
exclusive work queued ahead, and one that waits for a dead or uncertain guard entry counted as a
guaranteed slot keep nothing, as in admission; the reading runs no recovery census, so it counts
every such entry, and the next admission may remove one. A lane's own place in this Mac's queue,
which it keeps between offers, is not counted against it. Until vocaro-engineering#186 the reading
counted a guaranteed slot such a request kept as free, and queuekeeper offered work there that admission
then held back or that competed with the gate.

The reading applies admission's rule for an exclusive request queued there too: while one whose
process runs is the first request queued that holds later work back, admission holds every later
request behind it until it has been admitted and has ended, so no lane of any kind has spare
capacity there (`exclusive work is queued ahead on the second Mac: LABEL`). One whose process has
ended holds nothing back, and neither does one queued while a dead or uncertain guard entry is
counted as any slot, which it would wait for, as in admission (#89); one queued behind a
whole-host request that asks is that request's turn, read as the slots kept for it. Until
vocaro-engineering#191 the reading did not report such a request, and queuekeeper offered a lane to a Mac
whose admission then held it back for as long as the exclusive work waited and ran.

1. The second Mac runs the lane when its reading shows spare capacity and it can start the lane
   now: its queue admits the lane within five seconds (`qk run --if-free` there, which exits 75
   without running otherwise) and one of the project's four checkouts there is free.
2. When the second Mac's reading shows no spare capacity and this Mac's shows some, the lane goes
   to this Mac's queue at once, and nothing is offered: `queuekeeper: trying LANE on this Mac:` says why,
   and the receipt's `hereBecause` records it, naming the second Mac's CPU when that is it, as
   `the second Mac is busy (CPU 97% busy, at or above its HEAVY_BUSY_LIMIT=75%)`, else the
   whole-host lane or exclusive work running there, its guaranteed slots all busy, or those slots
   kept for a whole-host lane queued there, or exclusive work queued there. The CPU limit counts
   only when the lane would need a slot beyond the guaranteed ones: a free guaranteed slot is
   spare capacity however busy the CPU, as admission admits work into one. Until 2026-11-07 the
   reading refused a Mac whose CPU was over its limit even with guaranteed slots free, so the
   second Mac declined work it would have admitted, at 93% busy against its 90% with two of four
   guaranteed slots free, and this Mac's queue took it; the owner wants the second Mac loaded as
   much as possible, never the other way round. A second
   Mac whose reading shows capacity may still decline the lane as busy once it is offered, as
   when every checkout of the project there is in use or the model is held: this Mac's own
   admission then gets the same five seconds, and the lane runs here if it admits it, the receipt
   recording that the second Mac was busy. Busy is no configuration problem.
3. While neither Mac's reading shows spare capacity, `queuekeeper: neither Mac has spare capacity for
   LANE now (…), so it goes to whichever admits it first, the second Mac asked first` says so, and
   the lane is offered to the second Mac, whose guaranteed slots admit it however busy its CPU. While
   both decline it, the lane waits for whichever admits it first. It waits in this Mac's queue,
   15 s at first, doubling to 120 s, and between those waits both Macs are read again and the lane
   is offered to the second Mac again when the reading allows, so the second Mac is asked at least
   every two minutes; each offer costs the second Mac a checkout's claim, the project's `provision`
   lane and a policy refresh. An offer made again sends nothing a second time: the second Mac holds
   the commits after the first. `queuekeeper: LANE waits for whichever Mac admits it first:` says so, with
   why the second Mac has not taken it, and this Mac's admission prints what it waits behind
   ([heavy-work admission](#heavy-work-admission)). A second Mac that cannot be reached at one of
   these offers is asked again at the next, since one that went offline during the wait may come
   back before it ends: `queuekeeper: LANE waits for this Mac, and asks the second Mac again at the next
   offer:` says why, once for each reason. A lane that neither admits within 5,400 s ends with
   exit 75 (`BUSY`) and runs nowhere.

A gate that runs the project's suites side by side is a whole Mac's work: it says
`"wholeHost": true`, on the lane or on the group, and takes every guaranteed slot of the Mac it
runs on and holds that Mac until it ends, so that no other heavy lane runs beside it, a lighter
one included ([heavy-work admission](#heavy-work-admission), vocaro-engineering#196). A lane group that runs a
whole-host lane says `wholeHost` too, or is exclusive, as configuration validation requires; a
`self` lane cannot be whole-host, since it is not admitted, and an exclusive lane holds the host
already. Exclusive admission is unchanged. Each Mac's reading is `offload_host.py`'s `capacity`
answer, read in the second Mac's login shell as a run there reads it: its guaranteed slots and
their maximum, the slots busy among them, its CPU's busy share (`cpuBusy`, read over up to two
seconds when no kept sample spans one) and `busyLimit`, its load for the record, and the whole-host lanes and
exclusive work its admission guard records as running. A second Mac that cannot answer with a
reading runs the lane here, saying so; one whose admission limits cannot be read is misconfigured,
as every request there is refused ([heavy-work admission](#heavy-work-admission)).

A lane this Mac takes runs in this checkout, over its working tree as it is once admitted, not over
the commit sent to the second Mac. So when a commit was sent, this Mac runs the lane only while the
working tree is still that commit's tree: once admitted it snapshots the tree again, and when the
two differ it exits 2 without running, `queuekeeper: LANE not run on this Mac: the working tree changed
after COMMIT was sent` with the changed paths, rather than run a tree other than the one sent
([vocaro-engineering#79](https://github.com/vocaro/vocaro-engineering/issues/79)). Restore the
tree or run the lane again. Its receipt's `sent` names the commit, and `project` the tree as it stood
once admitted.

The toolkit it sent is not held to that rule. When the toolkit's working tree changed after it was
sent, the lane runs in this checkout with the toolkit as it was sent: queuekeeper says
`LANE runs on this Mac with the toolkit that was sent, COMMIT`, with the changed paths, makes a
detached worktree of the toolkit at that commit in a new temporary directory, and runs the lane
with `QUEUEKEEPER_ROOT` naming it. The worktree goes when the lane ends, and the toolkit's checkout
stays as it is. The receipt's `sentToolkit` names the worktree, the commit and the changes. A
session editing Queuekeeper's checkout, or a landing that moved it, used to end with exit 2 a gate
that had waited here for a Mac
([vocaro/queuekeeper#33](https://github.com/vocaro/queuekeeper/issues/33)). The `qk run` process
itself keeps the code it started with, and a project that reaches the toolkit by a relative path
rather than through `scripts/qk` still reads the checkout.

`qk admit` instead runs its command from what it sent, so that a wait here is not lost to an edit
the session made meanwhile: on 2026-11-07 a command the second Mac kept declining waited here
behind a gate and then refused over an unrelated documentation edit
([vocaro-engineering#276](https://github.com/vocaro/vocaro-engineering/issues/276)). While the tree is
still the one sent, the command runs in this checkout, as before. Once it is not, queuekeeper says so, with
the changed paths, and lays out what was sent as the second Mac lays it out: a detached worktree of
the project at the commit sent, in a new temporary directory under the main checkout's name, with
the toolkit beside it, where a project's relative paths to the toolkit find it (the toolkit's
checkout itself, by a symbolic link, while its tree is still the one sent, and otherwise a worktree
of it at that commit), and `QUEUEKEEPER_ROOT` naming it. The project's `provision` lane runs
there first, its log in `.build/queuekeeper/admit/provision.log`, and then the command, in the
worktree's directory that matches the current one. What it changed in files Git does not ignore
comes back as from the second Mac (below): applied here only while the tree here is still the one
sent, so usually kept as a patch, exit 73. Both worktrees and the directory are removed when the
command ends, however it ends, with the build products and other ignored files the command left
there; a build there starts cold, and what a tool writes outside the worktree, such as Xcode's
default DerivedData, stays. The receipt's `project` is the commit sent and its `sentTree` says
where it ran and what had changed. Nothing but that patch touches the session's tree. A lane still
refuses when the project's tree changed, as above: its results, kept results and logs are the
checkout's.

Each of this Mac's waits that ends unadmitted is logged as withdrawn, as each
declined offer is on the second Mac ([admission log](#admission-log)); every request retains
its arrival across retries, and keeps its place in this Mac's queue across local waits ([heavy-work admission](#heavy-work-admission)). `qk run --if-free`
asks each Mac once, five seconds each, and exits 75 when both are busy. The admission logs of
2026-10-27 hold this overflow before it was the rule: the build host's three `timed-out` requests
were `qk run --if-free` declining after 5.5 s, and each of those lanes then ran on this Mac
([vocaro-engineering#49](https://github.com/vocaro/vocaro-engineering/issues/49)).

First `qk run` checks, before it sends anything, that the second Mac can be reached: its name
resolves, an address of it answers on its ssh port within 3 s, and `ssh build-host true` finishes
within 30 s. The second Mac stays on its own wired network, while the owner takes this Mac to
others, where the second Mac's `.local` name does not resolve at all. The check then stops at the
name, after the 5 s that macOS waits for a multicast DNS answer that never comes (macOS 27.0.1,
2026-10-27), and sends nothing; an address that does not answer costs 3 s, not ssh's 10 s
`ConnectTimeout`. When this Mac is away from the second Mac's network in that way, when nothing
answers, or when ssh cannot connect, the second Mac cannot be reached, which is no configuration
problem: `qk run` prints one line, `queuekeeper: running LANE on this Mac:`, with why, and runs the lane
here, under this Mac's admission, at once (the owner's decision, 2026-10-27). It does the same
when the second Mac could not run the lane as it runs here: when the lane's arguments name a
physical device ([physical devices and the second Mac](#physical-devices-and-the-second-mac)).
A toolkit checkout with changes keeps no lane here: its working tree goes as a snapshot too.

A second Mac that answers but is misconfigured stops the lane instead, for the owner's decision
([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/agents/core.md#shared-resources)): when it refuses the login, as for a key
it does not accept or a host key that changed, its login shell fails, it cannot receive the
commit or run the toolkit, it refuses the lane for any reason but being busy, as when its Xcode
at this Mac's developer directory differs or every checkout holds changes a run left, or it fails
the project's `provision` lane. `qk run` runs nothing here then. It says what is wrong and what
fixing it takes, and exits 78 (`EX_CONFIG`). In the merge queue that exit is a run with no verdict,
run again once the host has settled, and the hold after the bound is for the owner, the one kind
whose cause is his to fix; a session that finds the cause gone still resumes it
([the merge queue](maintenance.md#the-merge-queue)). A run that was sent finishes there: a connection
lost once the work was sent leaves the run unmeasured rather than running the lane a second time
here, since it may still run there.

`qk run --here` keeps a lane on this Mac, for instance a build whose products the session uses
here, or, once the owner has decided so, any lane while the second Mac is misconfigured. It is not
for a second Mac that is busy or loaded: queuekeeper reads that itself and runs the lane here when this Mac
has the capacity, so a session that sees the second Mac loaded and this Mac idle runs
`qk run LANE` and asks nobody (the owner's decision, 2026-11-04,
[vocaro-engineering#168](https://github.com/vocaro/vocaro-engineering/issues/168)).
`qk run --offload-only` remains for work that must not run here, below.
`qk run --serial GROUP`, a group's serial reference, runs here too, and nested work runs inside
the admission it inherits, here. `qk run` looks for the second Mac only when
`QUEUEKEEPER_OFFLOAD_HOST` names one or `~/.ssh/config` defines the alias `build-host` or
`second-mac`, which `ssh -G` reports; `QUEUEKEEPER_OFFLOAD_HOST=none` keeps every lane here. A
lane's results come back to `.build/queuekeeper/remote/LANE/` when it ran on the second Mac and
stay in `.build/queuekeeper/LANE/` when it ran here. Every `qk run` ends with a short summary, on
stderr after stdout is flushed: its verdict, its passed, failed, skipped and unmeasured tests kept
apart, or that its log counts none, each failing test with its file and line, and the path to its
`failures.txt` or receipt (`lane_report`). The second Mac's own run leaves it to the Mac that sent it.
A lane group's results hold each of its lanes' log and receipt as well, under `lanes/` as on the
Mac that ran it, so here `.build/queuekeeper/remote/GROUP/lanes/LANE.log` and `LANE.json`. They
come back once the run there ends, whether the lane passed or failed, its timeout ended it or a
stop did. A lane's receipt gives its status, outcome, seconds and the executed and skipped tests
its log counts, so its counts read here; a lane a stop ended has the outcome `stopped` and no
status. A run whose connection is lost before it ends brings none back and is unmeasured; they
stay in the second Mac's checkout until a later run there replaces them.

Every run's receipt says where it ran. `ranOn` is `this Mac` or `second Mac`, which a receipt
written before 2026-10-28 gives as `build host`, and `mac` names the Mac. On this Mac,
`hereBecause` says why the run ran here: the lane's own reason, `--here` or `qk admit --here`,
`--serial`, nested work, a self lane's part, foreground work, no second Mac configured, the second Mac having no spare capacity (its CPU, a whole-host
lane or exclusive work running there, or its guaranteed slots all busy), the second Mac being busy
once offered, or why it could not be reached or could not run the work. The second Mac's own receipt, kept within the receipt that comes back,
names the Mac that sent the run in `dispatchedFrom`. A receipt holds its lane's last run only.
The share of heavy work each Mac runs over a period is in their admission logs, one line for
each admission: `qk admissions` here and on a copy of the second Mac's log counts each Mac's
admissions by lane ([admission log](#admission-log)).

The second Mac runs a lane in its own login shell's environment, not this Mac's, so a variable
set for `qk run` or `qk admit` here does not reach it. On 2026-10-27 a variable set for `qk remote admit`
read as unset there (toolkit `e137d7e`). A run that a variable selects, such as one that points a
package at a binary built here into an ignored path or sets an xcodebuild `TEST_RUNNER_` variable,
takes `--here` (`qk run --here`, `qk admit --here`), or names the setting in its command.
The loss is silent when the lane's script has a default: on 2026-11-11 a BreezEPUB purchase lane
given its simulator runtime in a variable ran on the default iOS 27.0, and only
the receipt's `Taking:` line showed it. A lane setting meant to reach the second Mac is an
argument after `--` (`qk run purchases -- --runtime ID`), and the receipt is where to confirm
it took effect.
What a project's extensions add to every command's environment (`extensions.COMMAND_ENVIRONMENT`),
such as the path of the toolkit's Swift package that XcodeGen expands, does reach it: each Mac's `qk`
sets it from its own checkout of the extension's folder, in place of any value the calling shell
set, so a path of this Mac's never reaches the second Mac
([vocaro/queuekeeper#97](https://github.com/vocaro/queuekeeper/issues/97)).

`qk admit -- COMMAND` goes as a lane does, at HEAD or a snapshot commit: to whichever Mac has
spare capacity for it, the second Mac first, to this Mac while the second Mac has none or cannot
be reached, and while neither has to whichever admits it first, within its `--timeout`;
`--if-free` asks each once (the owner's decision, 2026-11-07: all work first come, first served,
the second Mac first; until then only `qk admit --verify` was offered, and `--verify` is still
accepted, meaning the same). The two Macs share gigabit Ethernet, and a dispatch prepares and sends
in about 4 s. The command runs in the directory of the second Mac's checkout that matches the
current one, and its output and receipt come back to `.build/queuekeeper/remote/admit/`. queuekeeper keeps
it here, saying why, when the second Mac could not run it as it runs here: its directory is outside
the checkout or one Git ignores, which the commit sent lacks, or its words name this checkout's
path, a simulator by its identifier, which is a member of this Mac's pool, a physical device of
this Mac's record, or any other path of this Mac's alone: an absolute path outside the checkout,
such as a session's scratchpad, `/private/tmp`, `/tmp` or the toolkit's checkout, or one under `~`.
A relative path to a file in the checkout that exists and Git ignores, such as a session's
scratch script under `.build/issue-N/`, keeps it here too, since the commit sent lacks every
ignored file; an ignored directory does not, being most often output the command makes itself, nor does a
file a script writes after `>` or `tee`, such as its log (`remote.ignored_inputs`).
The system tools a command starts or uses as an interpreter are on both Macs, so a path under
`/usr`, `/bin`, `/sbin`, `/dev`, `/opt/homebrew`, `/Applications/Xcode*.app` or
`/Library/Developer`, or one that is what `PATH` resolves its name to, keeps nothing here. It stays
here too when this Mac's environment sets it up, since the second Mac's login shell has none of
it: any `TEST_RUNNER_` variable, or `DEVELOPER_DIR`, whose Xcode the second Mac would refuse as
misconfigured were its own at that path another. Nested in an admission, it runs inside that admission, here; a part of a `self`
lane stays on the Mac where the lane runs; foreground work runs where its foreground is held
([attended Mac sessions](#attended-mac-sessions)); and work the second Mac runs for this Mac runs
there. A test that a lane covers runs best as that lane, `qk run LANE -- FILTER`;
`qk remote admit -- COMMAND` sends a command on purpose, to run in the root of a checkout there.
With `--exclusive`, it is first offered to start at once (`--if-free`): the second Mac starts it only while
every slot is free and no request older than it waits there, and a declined offer leaves no place in its queue,
so the second Mac goes on taking other work. Offers come 15 s apart, doubling to 120 s, all carrying the
work's first arrival. After 900 s of declined offers (`EXCLUSIVE_PARK_SECONDS`), it takes its place in the
second Mac's queue at that arrival, and slots drain toward it as for any exclusive request, so it is not kept
waiting forever by a stream of later work. Foreground work with `--exclusive` still queues there at once.

`qk admit --here -- COMMAND` runs the command on this Mac and offers it nowhere, `hereBecause`
saying `qk admit --here`. It is for a command whose products the session uses here afterwards:
build output, DerivedData, a result bundle or other files outside version control, or an app it
then launches. Nothing about a command tells such a build from a test whose result is its log and
status, so the session says so. `qk app keep` builds the kept apps this way.

What a command that ran on the second Mac changed in the checkout's files that Git does not
ignore comes back and is applied here, so that `swift package resolve`, a formatter or a code
generator does not lose its edits. Once the command ends there, however it ended, the second Mac
stages the checkout's tree in a temporary index (tracked edits and deletions, and new untracked
files no `.gitignore` names, as a snapshot commit stages them), writes `git diff --binary` against
the commit sent to `changes.patch`, restores its checkout to that commit for the next run, and
sends the patch back with the receipt, into the dispatch's results under
`.build/queuekeeper/remote/.dispatches/admit/`. Here queuekeeper applies it with `git apply --binary`, to
the working tree and not the index, only while the working tree is still the tree that was sent,
and says which files it changed; the exit status stays the command's. When the tree here changed
meanwhile, or `git apply` refuses the patch (it applies all of it or none), queuekeeper applies nothing,
keeps the patch as `.build/queuekeeper/remote/unapplied/DISPATCH.patch`, says why and where it is,
and exits 73 (`UNAPPLIED`, sysexits' `EX_CANTCREAT`, which no other queuekeeper status uses); apply it with
`git apply --binary PATCH` once the tree allows, or run the command again. Files Git ignores, such
as `.build` and its build products, stay on the second Mac, and one line says so, naming
`qk admit --here`, which keeps them on this Mac.

A caller that holds or will take its own admission here, such as a push keeper that runs a gate
under the push's one admission, offers the lane with `qk run --offload-only LANE` instead:
`qk run` would run a lane it could not send under an admission of its own, and the caller would
then run it a second time. The second Mac takes it only if it can start it now, a tree with
uncommitted changes as a snapshot, so the status is that of the working tree the caller would
otherwise run; but it never runs here. The command exits 75 (`BUSY`) when the second Mac declines
the lane because none is configured, it cannot be reached or it is busy, and the caller then runs
the lane itself. It exits 78 when
the second Mac is misconfigured, which stops the caller too, and otherwise with the lane's status
there.

`qk doctor` compares the two Macs and reports a difference as an error: their default Xcodes
(`xcode-select -p` and its version), the Xcode at this Mac's developer directory, which every run
there uses, and the simulator runtimes that Xcode lists on each, as well as a second Mac that
answers but refuses the login. A second Mac that cannot be reached, as when this Mac is away from
its network, is no error, since work then runs here (the owner's decision, 2026-10-28): `unchecked`
says why it was not compared. A second Mac whose default Xcode differs still runs lanes, since a run selects this Mac's
developer directory, but whatever reads its default, such as `ssh build-host xcodebuild -version`
or a script without `DEVELOPER_DIR`, sees another Xcode. The two Macs keep the same runtimes,
and that is doctor's to fix, not the owner's (the owner's decision, 2026-11-03,
[vocaro-engineering#123](https://github.com/vocaro/vocaro-engineering/issues/123)): when one has a
runtime the other lacks, doctor evens them out as `qk remote setup --runtimes` does, deleting a
runtime that no simulator but Xcode's defaults uses and copying any other to the Mac that lacks it
([setting up the second Mac](#setting-up-the-second-mac)), with its progress on standard error, then
compares again. The difference is an error only when that fails. Doctor also shuts down the
[stale pool simulators](#stale-pool-simulators) on both Macs, as `qk simulator-pool reap` does, on
the second Mac only when it answered, and lists each stop under `offload`, `reaped`
([vocaro-engineering#127](https://github.com/vocaro/vocaro-engineering/issues/127)). Under `offload` in the report,
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
started over SSH, and from the second Mac's own pool, whatever its screen's state. It goes there like
any other heavy lane, and its first run there is its check (the owner's decision, 2026-10-28, in
place of the earlier rule that a simulator lane went there only once a run there had passed with
this Mac's test counts): counts that differ are a defect to fix there. One that also needs a
physical device stays here for that.

Each Mac keeps its own [pool](simulator-pool.md#reserved-simulator-pool) and its own
[device record](#device-reservations). A lane on the second Mac takes or creates members in the
second Mac's simulators and reserves them in the second Mac's record, so a member of the same
name on each Mac is two simulators, and a reservation on one Mac says nothing about the other's.
Physical devices, which both Macs reach, are reserved only in this Mac's record
([physical devices and the second Mac](#physical-devices-and-the-second-mac)).
A project's first run there creates its first members and lets their first boot finish. A
session's `QUEUEKEEPER_DEVICE_OWNER` and agent session do not cross SSH, so a reservation there names
an owner the run generates: `simulator-tests-UUID` for `qk simulator-tests`, and `POOL-UUID` for
a gate that takes its member with `queuekeeper.apple.simulators.pool.member()`.
`qk simulator-pool list` and `qk disk` show the pool of the Mac they run on; a run on the second
Mac prunes that Mac's members of the kind it takes.

Only logs and receipts come back: the lane's, or a group's and each of its lanes'.
`qk simulator-tests` leaves each device's log and result bundle in the second Mac's checkout,
where the lane's log names them, and puts a failing device's last 40 lines in the lane's log. A
session that needs a result bundle or a device log here runs the lane with `--here`.

Measured on 2026-10-27 with `qk remote run` from clean checkouts, against the same lane at the
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
the 23 minutes of tests that followed averaged 40. No run needed the screen unlocked. On that day
the boots, the builds and every gate, EPUBLib's Mac suite included, ran with the build host's
screen locked and the display on; the screen was unlocked, not by the run, for the last 15 minutes
of `ios-tests`. That was its state then, not now.

### How a run reaches the second Mac

`qk remote` runs a commit: HEAD when the working tree is HEAD's, and otherwise a snapshot commit
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
`qk run`, `qk admit`, `qk remote run` and `qk remote admit` all send work this way.
The toolkit's checkout goes the same way, as a snapshot of its working tree when it has changes,
untracked files included, rather than keep the work here (owner's decision, 2026-11-02,
[vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)); admission
chooses the Mac. A checkout that is clean, on `main` and only behind its `origin/main`, as last
fetched, sends `origin/main`'s commit instead of its HEAD, and the dispatch says so in one line
(`sending the toolkit at origin/main's COMMIT, not the shared checkout's HEAD OLD, which is clean,
on main and N commits behind it`). The shared checkout waits behind while a process runs from it
([guides/maintenance.md](maintenance.md), The shared checkout follows each toolkit landing), and
a project change that needed a just-landed toolkit API failed on every run sent from this Mac
until it followed. Such a run there executes landed code that a run here does not yet read, which
replaces #104's rule that a dispatch sends what a run here would read (owner's decision,
2026-11-07, [vocaro-engineering#227](https://github.com/vocaro/vocaro-engineering/issues/227)). A
checkout with changes, on another branch, current, or with commits of its own sends its own tree
as before, and a toolkit so sent that is behind its `origin/main` is named in one line (`the
toolkit sent, …, is N commits behind its origin/main`). Work this Mac takes after offering it runs
while the toolkit checkout still holds the HEAD it had, as a run here reads the checkout. Nothing
the second Mac does needs a toolkit commit a branch holds: the policy refresh there reads the
toolkit's `origin/main`, not the commit checked out. A queue's gate uses a clean copy of the toolkit instead ([which toolkit a gate runs](maintenance.md#the-merge-queue)).

`qk remote` takes no slot here. It pushes the commit, and the toolkit commit that runs it, by git
over SSH and without hooks to bare repositories under
`~/Development/build-host` on the second Mac, so nothing reaches GitHub. The connection that
prepares them first lists the processes earlier runs left running there
([processes left over from runs](#processes-left-over-from-runs)). There `offload_host.py`,
sent along and run by the host's own Python, takes one of four persistent checkouts of the
project, checks the commit out detached with the toolkit beside it, and runs `qk run`, or
`qk remote admit`'s command, in the checkout's root, or `qk admit`'s command in the directory that matches
the one it was sent from. The work it runs knows it runs there for this Mac, whose name
`QUEUEKEEPER_DISPATCHED_FROM` gives, and its receipts say so. A checkout keeps its ignored inputs and
build products, so its next build is warm; `qk remote` waits for one when all four are in use.
Of the free checkouts, a run takes the one whose last tree is nearest its commit's, by the files
that differ between the two, so its builds redo the least, and the log says which slot it took and
how many files differ (`slot 1: its last tree is the commit's, so its builds are warm`). A slot
whose tree cannot be compared, or that has no checkout yet, comes after the rest, in index order.
In index order alone, PDFReflowLib's queue gate there spent 406 to 733 s in its first step on a
slot that had last built an older tree, against 362 to 365 s warm (2026-11-05;
[vocaro-engineering#183](https://github.com/vocaro/vocaro-engineering/issues/183)).
A project's `provision` lane, when it has one, runs first to provision the checkout's ignored
inputs, and the work runs only if it passes. It runs there as `qk run --here provision`, admitted as
every lane is, and with `--if-free` when the work was offered to start now, so that a second Mac with
no free slot declines the work rather than hold the checkout while the provision lane waits. The work runs with `PYTHONDONTWRITEBYTECODE=1`, so
its Python leaves no `__pycache__` in the checkout, which a project's `.gitignore` need not name;
only a process that sets its own environment can still write one. A checkout that a run left with
changes stays as it is for inspection and is passed over. Once it is read, remove it with
`git --git-dir ~/Development/build-host/repositories/NAME.git worktree remove --force PATH`.

#### A named checkout slot

`qk remote run LANE --checkout-slot N` and `qk remote admit --checkout-slot N -- COMMAND` run in
checkout N, 0 to 3, and no other, for a check that depends on the checkout's absolute path, warm
products or generated project. Repeat one across slots by running it once per slot, each named:

```sh
for slot in 0 1 2 3; do python3 scripts/qk remote run LANE --checkout-slot "$slot"; done
```

The run waits for that slot under the same lock as any other, even while other checkouts are free,
and says who holds it (`checkout slot 2 of NAME is in use, held by PID 123, started ...`). The waiter
has no place in line: the lock goes to whichever run asks once it is free, so a run with no named
slot can take it first. The wait is bounded, by `--checkout-wait SECONDS` or else 5400 s; one that
runs out exits 75, `not run`, having run nothing. A number outside 0 to 3 or that is no number is
refused before anything is sent. A named slot with changes a run left is refused with what
removes them, since no other slot may stand in and waiting would not clear them. The receipt's
`checkoutSlot` gives `requested`, `assigned`, `path` and `waitSeconds`. Without the option, a run
takes the nearest free checkout as above.

A checkout slot says where a run's files are. It is not a heavy-work slot, which says how much of
the Mac the run may use and which `qk admit` waits for only after the checkout is held, and it is
not the foreground reservation, taken at that same later point. So waiting for a named checkout
holds neither. It does not choose the Mac either: `qk remote` always means the second Mac.

A push learns what the second Mac has only from the refs its repository advertises, and a
checkout's detached HEAD is none of them. Placing a commit deletes the ref that carried it, so a
repository whose checkouts alone hold its commits has a push send the commit's whole history,
however much of it they hold. Against a stand-in second Mac on this Mac, an unchanged commit
holding 24 MiB of random data and a one-line change on it each sent 24.01 MiB to such a
repository (Git 2.54.0, Apple Git-157, 2026-10-29;
[vocaro-engineering#67](https://github.com/vocaro/vocaro-engineering/issues/67)). So each
repository there names each commit its checkouts hold as `refs/queuekeeper/checkouts/COMMIT`, no more
refs than checkouts. Placing a commit names it, and the preparing connection names them all
again before the pushes, so a repository whose checkouts an older queuekeeper placed, or that lost one, is
current. A push then sends only what those commits lack: nothing for a commit a checkout holds,
282 bytes for that one-line change. It is warm when this Mac has one of the commits the second
Mac named, and cold when it has none, as for a project's first push or once its checkouts are
gone. A cold push sends the whole history although the objects of earlier pushes may still be
there, unreferenced.

Dispatches that send one commit at once, as two lanes started together from one checkout do with
the toolkit's commit, race for its ref, and the push that loses fails with `cannot lock ref …:
reference already exists`. queuekeeper then asks the repository with `git ls-remote` for both names the
commit can have there, the pushed ref and the checkout's, and when either names it, the push has
sent it: queuekeeper says `holds COMMIT all the same` and the dispatch goes on. The winner's dispatch may
already have placed the commit, deleting the pushed ref, and not yet named it as a checkout's; on
2026-11-04 that left neither name, and a BreezEPUB lane stopped with exit 78 and the advice to
repair the repository or free space
([vocaro-engineering#152](https://github.com/vocaro/vocaro-engineering/issues/152)). So a push
that lost the race to a commit named nowhere is made again, three pushes in all, half a second
apart and then a second: one that finds the ref gone creates it, sending nothing. When each loses
and the commit is still named nowhere, the lane stops naming the race, with nothing on the second
Mac to repair: send it again, or start such dispatches a few seconds apart. A pushed ref that names
another commit, or a failure that is no race, such as a full disk, is not pushed again and stops
the lane as before. The push's record gives `race` when an earlier push took the ref, and
`attempts` and the earlier pushes' lines (`raced`) when it was made again.

`push.negotiate=true` finds such objects too, through a fetch exchange before the push, but only
over protocol v2, which reaches the second Mac only as the `GIT_PROTOCOL` variable that ssh sends.
A stock macOS sshd accepts only `LANG` and `LC_*` (`AcceptEnv` in
`/etc/ssh/sshd_config.d/100-macos.conf`, macOS 27.2). Without it, Git says `--negotiate-only
requires protocol v2`, `fatal: the remote end hung up unexpectedly` and `push negotiation failed;
proceeding anyway with push`, and pushes as it would without the setting, successfully (measured
with the stand-in dropping the variable). queuekeeper sets neither it nor any other Git setting: the refs
make a push warm over any protocol. Whether the second Mac's sshd passes `GIT_PROTOCOL` is
unmeasured.

The receipt's `preparation` records what came before the second Mac's queue: the seconds of
staging the commits, of the preparing connection and of the pushes, both Macs' `git --version`,
and each push. A push's record gives its status and seconds; its phases, from when each line of
`git push --progress` arrived: `connect`, until Git starts the pack, after the second Mac's ref
advertisement; `pack`; `transfer`, until the pack's last byte is written, which the second Mac
indexes as it arrives; and `receiver`, until the second Mac reports the ref, which takes the rest
of its indexing and its connectivity check. It gives the objects and bytes Git reports, the bytes
to Git's rounding, whether the push was warm or cold, the `push.negotiate` setting it read, and
every line it wrote beyond its progress and the ref's report, which queuekeeper also prints, such as a
negotiation's failure. The result's `phases` give the second Mac's wait for a checkout, placing
the commits there and each queuekeeper command, and the work's own receipt gives `queueSeconds`, its
wait in the queue, as a receipt of a run on this Mac does. queuekeeper prints a line for each push and
one for the preparation and the queue's wait before the result.

A push that has not finished in 30 minutes is stopped, as is one that a stop interrupts: queuekeeper
ends git and the ssh it started at once, and nothing of the push enters the second Mac's
repository. Stopped at its bound, it stops the work for the owner, as a push that fails does:
nothing ran there. Either way, a record of what was sent, unique to the dispatch, says how far the
push got. receive-pack there may die of `SIGPIPE` once its connection has closed and leave its
quarantine, `objects/tmp_objdir-incoming-*`, with what had arrived. That directory is no part of
the repository, and Git's prune removes it once it is older than `gc.pruneExpire`, two weeks
unless set (Git 2.54.0).

Output comes back as it is written. The lane's log, the command's output and a receipt land in
`.build/queuekeeper/remote/LANE/` (`remote/admit/` for a command), and the exit status is the
second Mac's: the work's, and 1 for a run stopped before the work began, even after the provision
lane passed (vocaro-engineering#317). A lane group's lanes' logs and receipts follow once the run there ends, a stopped
run's included, into `lanes/` there, and the receipt's `returned` lists those that came. Before
the run, the second Mac removes those its checkout holds from an earlier run, so none comes back
as this run's. The receipt names the second Mac's model, macOS and Xcode beside this Mac's. The
second Mac must have the same Xcode build at this Mac's developer directory, or the run is
refused. A different macOS is reported but refuses nothing: a whole lane's pass there counts
for a push from this Mac, while as a qualification claim it names the second Mac's platform. A
whole lane's receipt is also kept by commit in the repository's Git directory, so
`qk remote passed LANE` answers from any worktree whether the lane, without arguments, passed
at exactly that commit. A whole lane's run on this Mac, at a commit with no working changes and
by a toolkit with none, is kept there too, with `ranOn` `this Mac` and the worktree it ran in, so
that a gate the merge queue ran here counts from a session's checkout
([vocaro-engineering#198](https://github.com/vocaro/vocaro-engineering/issues/198)). A later run
of the lane at that commit, on either Mac, replaces it. What the run wrote in its `results/` is
kept beside the receipt, in `LANE.results/`
([a lane's results](parallel-tests.md#a-lanes-results)). A push path that
accepts it in place of running the gate again prints both Macs' macOS versions from the receipt.

Each dispatch also keeps its own copy of its results, in
`.build/queuekeeper/remote/.dispatches/LANE/TIME-PID/` (`admit` for a command), which the line
that gives the run's status names. `LANE/` holds the copy of the dispatch that ended last, so two
dispatches of one lane, or two commands, at once each keep their own. Each name keeps its newest
eight, two for each checkout there, and queuekeeper names each one it removes. Dispatches of one name file
their results in turn. Replacing `remote/admit/` by removing it and renaming another into its
place fails with `Directory not empty` (Errno 66) when two dispatches interleave, and left one
dispatch's results unfiled
([vocaro-engineering#67](https://github.com/vocaro/vocaro-engineering/issues/67)).

The receipt names the commit that ran (`commit`) and the HEAD it was sent from (`head`), the same
commit unless `snapshot` is true, and likewise the toolkit's: `toolkit`, `toolkitHead` and
`toolkitSnapshot`, with the toolkit checkout's own HEAD beside them as `toolkitCheckoutHead`, which
differs from `toolkitHead` only when `origin/main` was sent in its place. A snapshot's receipt, of
either, stays in `.build/queuekeeper/remote/LANE/`: no branch holds the snapshot, so its result is
no commit's pass, and `qk remote passed` never counts it, for HEAD or for the snapshot itself. Commit the work and run the lane again for a pass
that counts. `qk run` and `qk remote run` say so as they send a snapshot of the whole lane,
naming the changed and untracked files, as `qk run` does for a run here on such a tree
([vocaro-engineering#197](https://github.com/vocaro/vocaro-engineering/issues/197)).
`qk remote run --serial GROUP` runs a group's lanes one after another there, under
the second Mac's admission: the group's serial reference
([parallel-only failures](https://github.com/vocaro/vocaro-engineering/blob/main/guides/parallel-tests.md#a-failure-seen-only-side-by-side)). Its receipt says
`serial` and, like a snapshot's, stays in `.build/queuekeeper/remote/GROUP/`: it is no pass of the
group's, whose lanes run side by side.

A stop carries over: `qk remote` tells the second Mac to stop, the run stops as a stop signal
stops a local one, and it leaves the second Mac's admission. A connection's end is no stop
([vocaro-engineering#321](https://github.com/vocaro/vocaro-engineering/issues/321)). When the
dispatching Mac sleeps, or the connection otherwise drops, a run that has started goes on, and
`qk remote` reconnects, again after each failure, once it is awake, and collects the run's log,
result and returned files from a journal the second Mac keeps under `dispatches/` in its
remote root, as if the connection had stayed up: the run passed, failed or was unmeasured by its
own receipt, and that receipt's `connection` says it was collected on a later connection. A stop
asked for meanwhile reaches the run on the next connection that gets through. Two exceptions end
the run with its connection as before: a run that carries a device reservation, since this Mac's
record holds that (see [Physical devices](#physical-devices-and-the-second-mac)), and a run whose
dispatcher had not yet heard it start, which it counts as never run.

The second Mac cannot tell a dispatcher that died from a Mac asleep or a dropped connection, so
the dispatching Mac decides. A dispatch that may keep a journal tries to record its journal
identifier, host, PID and process start time under `dispatchers/` in this Mac's durable state
(`remote.dispatch_records`). It keeps the record if it cannot collect a known journal's result or
learn that the journal is gone. The next `qk remote` dispatch or queue worker start looks for a
dispatcher proven gone, with no process at that PID or one that started at another time, as after
a reboot. It sends `stop` for that journal alone through `reattach`. A run still active leaves
admission and frees its checkout when it handles the stop, with its journal kept a week. A
dispatcher still alive, as on a sleeping Mac, is left alone. If the second Mac cannot answer for
the record, it stays for another attempt and queuekeeper reports that it could not request the stop.

For a run whose connection's end is a stop (one with a device reservation, or one before its
dispatcher heard it start), a Mac that crashes or loses power closes nothing, and the second
Mac's sshd would learn of it only from TCP keepalive. So the dispatcher writes a heartbeat on the
run's standard input every 20 s, and the second Mac stops such a run after 180 s with none
(`remote.HEARTBEAT_SECONDS` and `HEARTBEAT_SILENCE`): SIGTERM to the run's own process group,
which leaves admission, and then the checkout's slot comes free. It sends no result and drops the
run's output from then on, so a dispatcher that wakes reports the run unmeasured. 180 s outlasts
ssh's own two-minute bound. A journaled run its dispatcher has heard start is never stopped by
silence (vocaro-engineering#287, #321).

A run nobody is attending still ends. `qk run` and `qk admit` on the second Mac stop their own
work at its timeout, as ever, unmeasured. Past that, once a connection has been lost, the second
Mac itself stops whatever is left after the lane's timeouts, or `--time-limit`, plus 95 minutes
for a checkout and admission, SIGKILL following five minutes later; the result says `detached`,
and `overran` when that bound stopped it. A run that returns no result on any connection, within
that bound of this Mac's waking time, is unmeasured, never passed or failed, and the message names
what the second Mac kept. Foreground lanes run only on this Mac, and the policy says what else
stays here.

ssh closes a connection that has heard nothing from the second Mac for about two minutes
(`ServerAliveInterval` 30 s, four times), so a shorter silence leaves the run going. When a run
returns no result, `qk remote` says what ssh last said, such as `Timeout, server NAME not
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
nothing, so a timeout that this Mac slept through says nothing about the second Mac, and queuekeeper then
says to keep this Mac awake rather than to wake the second Mac.

On 2026-10-27 the build host accepted `qk run`'s first connection at 22:13:09 UTC, and this Mac
then slept until 22:16:39: its lid had closed at 22:11:35, and its power log gives
`Thermal Emergency Sleep` for each sleep after the first. The build host ended that connection, never
authenticated, at 22:15:12, about sshd's default `LoginGraceTime` of 120 s later. ssh's two-minute
timeout ran out at 22:18:38, two minutes of awake time after the connection began. ssh had said
`Connection to ADDRESS port 22 timed out`, and queuekeeper said to wake the build host, which had stayed
awake and on its network.

### Local Network access

macOS's Local Network privacy decides whether a process may connect to the second Mac by the process macOS holds
responsible for it, not by the process itself. The Claude app starts each session's `claude` through its `disclaimer`
helper, which makes that `claude` responsible for everything the session starts, a process detached with
`start_new_session` too, such as the queue worker `Queue.start` starts. While that `claude` runs, its grant covers them.
Once it exits, as when its session is archived, macOS holds each such process responsible for itself, a Python, and
from then on every connection it makes on the local network fails at once with `EHOSTUNREACH`, `No route to host`,
while every other session's processes still reach the second Mac (vocaro-engineering#282).

On 2026-11-07 a probe that connected to the second Mac's port 22 every 30 s, started detached from a throwaway
`claude -p` run through the app's `disclaimer` helper, connected at 16:37:48; that `claude` exited at 16:37:52, and
from 16:38:18 on every connection failed with `No route to host`. The same probe started from a live session kept
connecting. At 16:38:18 the system log had `UserEventAgent` say `Got local network blocked notification` for the
exited `claude`'s PID, then `Failed to get the signing identifier ... No such process`. `nehelper` still said `Local
network allowed by preference` for `com.anthropic.claude-code`, and the connections stayed blocked. Run as a launchd
agent (`launchctl submit`), the same probe was blocked from its first try, under Homebrew's Python and under
`/usr/bin/python3` alike, though `nehelper` said the owner's preference allowed Homebrew's Python
(`org.python.python`, which has no team ID).

queuekeeper tells this from a second Mac that is away: when a connection to the second Mac fails, the reach check asks macOS
which process is responsible for its own (`responsibility_get_pid_responsible_for_pid`), and when that is a Python, the
process itself or a worker above it, it raises `NoLocalNetwork`, saying that this process has lost Local Network
access, and searches for no renamed second Mac. A queue worker asks before each round, while a second Mac is
configured, and on such an answer exits, holding nothing, with its entries back in the queue. The next `qk land`,
`qk queue wait` or `qk queue resume` from a live session starts a worker that reaches the second Mac. A session
that finds the queue waiting with no worker runs `qk queue resume`.

A queue can use a signed launcher app as the responsible process. A per-repository LaunchAgent runs the app directly
and names its bundle ID in `AssociatedBundleIdentifiers`. The app stays alive while its Python worker runs, including
when the worker replaces itself with a newer toolkit. On 2026-11-09, a Developer ID–signed LaunchAgent probe connected
to the second Mac on all 192 attempts over 122 seconds after its starter exited. A matched probe detached from a second
instance of the same app connected eight times while its launcher lived and failed all 144 attempts after the launcher
exited and macOS attributed it to Python (vocaro-engineering#299). See [Signed queue launcher](maintenance.md#signed-queue-launcher)
for setup and recovery. Apple describes the bundle attribution and signing requirements in
[TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).

A worker whose session ends during a gate exits too. The `qk run` of each of the gate's lanes offers the lane to the
second Mac first, and when that offer gets `NoLocalNetwork` it runs nothing on this Mac and exits 69 (`EX_UNAVAILABLE`).
The worker reads that as no verdict and exits as it does before a round, without counting the run toward
`queue.noVerdictRuns`. Only the `qk run` a queue worker starts for a gate's lane does this: a session's own `qk run`
keeps its fallback, as does a second Mac that is only away (vocaro-engineering#300).

### Extension folders beside a project

A project's extension may load from a folder beside the project, named in `queuekeeper.json` as
`{"module": NAME, "path": "../FOLDER/..."}`. The second Mac is not sent that folder. Each dispatch links
the second Mac's own `~/Development/FOLDER` beside the dispatched checkout, so the extension runs as that
Mac holds it, and the connection that prepares the dispatch fast-forwards that checkout to the commit
this Mac's checkout of it names as `origin/main`, under the conditions below for the second Mac's own
toolkit checkout. On this Mac, a project whose `main` sets `"queue": {"followLandings": true}` has its
main checkout fast-forwarded after each landing in the same way, for a checkout other projects read.

So a run on the second Mac reads the extension as that Mac's checkout holds it, not as the commit under
test or this Mac's checkout has it. The fast-forward happens only when that checkout is on `main`, clean
and holds no commit of its own; one that is dirty, on a branch or diverged is left as it is, the
dispatch says so, and runs there use it as it stands. A change to the extension is therefore tested on
the second Mac only after it has landed.

### The second Mac's own toolkit checkout

The second Mac has a toolkit checkout of its own at `~/Development/queuekeeper`. No dispatched
run uses it: each run uses the toolkit checkout dispatched beside its project, at the commit that was
sent. Anything that runs `queuekeeper` straight from that checkout does use it, such as a
[recovery](#stale-pool-simulators) run there over ssh. On 2026-11-05 it was at e097b91, about 195
commits behind, and it lacked `qk admissions limits`. Its own `origin/main` was stale too, since
nothing there fetched it. queuekeeper now keeps it current (owner's decision, 2026-11-05: "it should be
auto-updated by the queuekeeper scripts";
[vocaro-engineering#203](https://github.com/vocaro/vocaro-engineering/issues/203)).

The update runs in two places:

- The connection that prepares each dispatch, so every `qk run`, `qk admit`, `qk remote`
  and merge-queue gate sent there. The dispatch passes the commit this Mac's `origin/main` names, and
  a checkout that already holds it counts as current without a fetch.
- `qk doctor`, in what it fixes on both Macs (`cli.fix_hosts`). It always fetches.

offload_host fast-forwards the checkout to `origin/main`, without hooks, under the same conditions as
this Mac's shared checkout follows a landing
([guides/maintenance.md](maintenance.md), The shared checkout follows each toolkit landing):

- it is on `main`;
- it has no changed, staged or untracked path (ignored files stay);
- it holds no commit that `origin/main` lacks;
- it is behind; and
- no process of the account runs from its tree. That means a working directory or an argument below
  it, outside session worktrees inside it and Git's own directory. A process that only names the
  checkout itself does not count, and open files are not read.

Otherwise it leaves the checkout as it is and says why. A dispatch prints one line,
`remote: left HOST's toolkit checkout ~/Development/queuekeeper at COMMIT as it is: REASON`,
and goes on. `qk doctor` reports the same line as an error. Reasons include a missing checkout,
another branch, a detached HEAD, changes, commits of its own or a diverged history, a process running
from it, and a fetch that failed or timed out. These are the owner's to decide. A session does not
stash, reset or edit that checkout to make it follow. An update prints
`remote: updated HOST's toolkit checkout … from OLD to NEW, N commits of origin/main`. A current
checkout says nothing.

Limits:

- **Time.** All of the update's steps share one 40 s deadline, the fetch at most 25 s of it. Each git
  step runs in a process group of its own, which the deadline kills.
- **No prompts.** Git cannot prompt: `GIT_TERMINAL_PROMPT=0`, and ssh runs in BatchMode. The fetch
  uses `~/Development/build-host/git-credentials`, as runs do, when that file exists.
- **Concurrency.** A lock under `~/Development/build-host` lets one connection update at a time. Any
  other connection skips the update while it is held.
- **Failures.** Whatever goes wrong becomes the answer's reason, so the update never ends or fails
  the connection that prepares a dispatch.

`tests/host_isolation.py` replaces `cli.fix_hosts`, so no doctor test updates the real second Mac,
and a test that dispatches uses a fake second Mac or `QUEUEKEEPER_OFFLOAD_HOST=none`.

### Setting up the second Mac

Start with `eng setup --check` from the workstation. It reports local readiness and checks
the configured build host over trusted SSH without applying changes. Use `--local` for one
Mac, or `--signing` to include the build host's kept keychain setup. The report identifies
which checks are incomplete and points to the owner-run procedures below. It checks that the build
host can read the private toolkit. The keychain setup check inspects file metadata. Use a signing
workflow to prove key use.

The owner prepares the second Mac once:

1. Turn on Remote Login (System Settings, General, Sharing) and add this Mac's public key to the
   second Mac's `~/.ssh/authorized_keys`.
2. Name it in this Mac's `~/.ssh/config`, so that `ssh build-host true` succeeds without a
   prompt. queuekeeper takes the alias `second-mac` as well, in place of `build-host` or on the same
   `Host` line beside it, and tries `build-host` first ([earlier names](#earlier-names)):

   ```
   Host build-host
       HostName NAME.local
       AddressFamily inet
   ```

   `AddressFamily inet` keeps ssh to IPv4. The build host's `.local` name resolved first to an
   IPv6 link-local address that never answered, so each connection waited out `qk remote`'s
   `ConnectTimeout` of 10 s before trying IPv4: about 10 s for every connection, four for a
   project's run, where it now takes 0.3 s (2026-10-27). An ssh without `ConnectTimeout`, such as
   a plain `ssh build-host`, waited out macOS's own TCP connection timeout instead, 75 s
   (`net.inet.tcp.keepinit`). Neither Mac reached the other over IPv6 at all, while IPv4 worked;
   that is the network's, which is the owner's. `qk doctor` measures each address ssh tries, and
   reports first addresses that keep every connection waiting as an error
   ([which Mac runs a lane](#which-mac-runs-a-lane)).

   macOS renames a Mac whose `.local` name another machine on the network claims, adding or
   raising a `-N` suffix (`-3` became `-4` on 2026-11-01). The old `HostName` then does not
   resolve, or resolves to the other machine, which presents another host key. queuekeeper finds the
   second Mac again in either case: Remote Login still offers ssh over Bonjour, under the
   computer name, so queuekeeper resolves each Mac that `dns-sd -B _ssh._tcp` lists to its current name
   and asks it, as ssh would ask the old name, to run `true` while presenting the key
   `known_hosts` holds for the old name (or for `HostKeyAlias`). That key is the second Mac's
   identity: when exactly one Mac presents it, queuekeeper uses that Mac's name for every connection of the
   run, says so in one line, `queuekeeper: build-host was renamed: …`, and `qk doctor` reports the rename
   as an error until `~/.ssh/config` names it. None, or more than one, is a second Mac that cannot
   be reached, and the failure says what the search saw: that Bonjour lists no other Mac offering
   ssh, so the second Mac is away or asleep, or which Macs it lists, none presenting the key. The
   search costs about 2 s more when this Mac is away from the second Mac's network, after the 5 s
   the name's lookup already waits; finding a renamed Mac took 7.4 s in all (macOS 27.0.1,
   2026-11-01).

   Every ssh connection queuekeeper makes searches this way when its name does not resolve or answers with
   another key, since ssh then logged in nowhere and nothing ran: the check before a dispatch, a
   foreground lane's preparing connection, `qk remote foreground` and the other questions queuekeeper asks
   the second Mac. On 2026-11-03 the second Mac became `-5` while only the check before a dispatch
   searched, and that search found nothing before 10 a.m.: `dns-sd` pads an hour of one digit with a
   space (` 9:46:14.615  Add …`), which queuekeeper did not read as a Mac. Lanes fell back to this Mac, and
   foreground lanes and `qk remote foreground` told sessions to wake a Mac that was awake. With both
   fixed, a search from this Mac with `-4` configured found `-5` by its key in 7.2 s (macOS 27.2,
   2026-11-03; vocaro-engineering#129).

   To keep the new name, put it in `HostName`, and add `HostKeyAlias` with the name its key is
   recorded under, the old `HostName`. ssh then checks the key by that alias, whatever the
   `HostName`, so a later rename needs only the new `HostName`, not a new `known_hosts` entry.
   Giving the second Mac a `LocalHostName` no other machine is likely to claim (`sudo scutil
   --set LocalHostName NAME` there) makes renames rarer, though macOS can still rename it. That
   setting, and `~/.ssh/config`, are the owner's: queuekeeper changes neither, needs `sudo` for the first,
   and says what to set. Nothing else in queuekeeper depends on the old name.

3. Install the Xcode this Mac uses at the same path and follow the first-launch instructions
   from the setup report.
4. Install Homebrew there (https://brew.sh); its installer asks for the second Mac's password.
   Then, from this Mac, install the tools the projects' gates use:

   ```sh
   python3 scripts/qk remote setup [FORMULA ...] [--import MODULE ...]
   ```

   It puts Homebrew on the login shell's PATH in `~/.zprofile`, since `qk remote` runs in the
   second Mac's login shell, and installs Homebrew's Python, `uv`, `shellcheck`, `xcodegen`,
   `zstd` and each formula a project names. A new login shell then checks every one, each
   `--import` module in that Python, and the Xcode at this Mac's developer directory, one line
   each, and the command fails while anything is missing. A second Mac without Homebrew stops it
   with nothing changed. `--check` only checks. A project's runbook names its own formulae.
   Without Homebrew, the system's Python 3.9 runs the toolkit, and lanes that need a newer
   Python fail.
5. Give both Macs the same simulator runtimes, and again whenever either gains one:

   ```sh
   python3 scripts/qk remote setup --runtimes [--check]
   ```

   It gives both Macs the same runtimes of the Xcode at this Mac's developer directory, as
   `qk doctor` compares them, and `qk doctor` runs it itself when they differ. A runtime that only
   one Mac has is deleted from that Mac when no simulator but Xcode's defaults uses it there, read
   again just before the delete (`xcrun simctl runtime delete`), so a runtime nobody uses costs
   neither Mac its space; one that something uses, or whose simulators simctl cannot list, is
   copied to the other. A superseded build, one whose identifier CoreSimulator resolves to another
   build by the rule `qk disk` uses (Reclaiming disk space), runs none of its identifier's
   simulators, so none uses it, unless the toolkit pins it or that Mac's device record names it,
   as `qk disk` keeps it; an unreadable record keeps every superseded build used. This Mac sends
   its pins with each request, since the second Mac reads nothing of the toolkit
   ([#190](https://github.com/vocaro/vocaro-engineering/issues/190)). Both must have that Xcode at
   the same path. The
   copy is the runtime's own disk image, as `xcrun simctl runtime list -j` names it (for a
   cryptex, the one disk image of its listed size in its asset's `Restore` folder), so a beta
   build Apple no longer serves comes across exactly. It goes over the second Mac's SSH
   connection, never GitHub, into `~/Development/build-host/runtimes` on the second Mac or
   `~/Library/Application Support/queuekeeper/runtimes` on this Mac, outside
   `/private/tmp`, which a reboot empties. Only an image of exactly its listed size is added,
   with `xcrun simctl runtime add --move`, which leaves no second copy. A copy that fails or is
   stopped adds nothing and leaves nothing staged, and the next run removes what a reboot or a
   killed run left. Only a runtime whose image cannot be read is downloaded instead, by its
   build (`xcodebuild -downloadPlatform PLATFORM -buildVersion BUILD`). Before each copy or
   download it measures the free space where it lands and refuses one that would leave less
   than 40 GiB: room for simctl to copy the image once more where it cannot clone it, and for
   other sessions' building worktrees. It reports each runtime's size, copy and addition, then
   lists both Macs' runtimes again and exits 0 only when they match, otherwise 1 with what still
   differs. `--check` says what it would delete, copy, download or refuse, changes nothing, and exits 1
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

8. For work that signs, keep the second Mac's login keychain unlocked for `qk remote`'s runs:
   from this Mac, in your own Terminal, run `python3 scripts/qk keychain keep-unlocked` once and
   type the second Mac's login password at its prompt
   ([keeping it unlocked](#keeping-the-second-macs-keychain-unlocked)).

The project's commits reach the second Mac over SSH without a GitHub credential. A heavy lane's
policy refresh fetches the private toolkit over HTTPS and needs read access to it through the
credential file above, even when the project has no private package dependencies. SwiftPM
clones a private dependency with git, and git's default helper on a Mac, osxkeychain, cannot read
the keychain from an SSH session: it fails with `-25308`, and the clone fails
([keychains on the right Mac](#keychains-on-the-right-mac)). When
`git-credentials` exists, `qk remote`
gives each run's git that file through the store helper and no other helper, by
`GIT_CONFIG_COUNT` variables that reach only the run, so the owner's own git configuration on
the second Mac is unchanged. A token reaches only the repositories it names, read-only, and it
expires: renew it before then. Check the second Mac with a short lane first, such as this
toolkit's `python3 scripts/qk remote run gate`.

Private GitHub release assets need the authenticated API as well as Git source access.
Declare their packages in `githubArtifacts`; admitted builds use the shared
[artifact preparation](https://github.com/vocaro/vocaro-engineering/blob/main/guides/public-access.md#private-binary-artifacts-and-build-credentials), whose
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
python3 scripts/qk keychain recover [--target MAC] [--keychain PATH] [--identity NAME]
python3 scripts/qk remote admit -- python3 scripts/qk keychain check [--keychain PATH] --identity NAME
```

`qk keychain recover` is the owner's, and unlocks nothing and reads no password. It:

- names this Mac, by the LocalHostName that receipts record, and the kind of session it runs in;
- finds the target, by default the second Mac, through the resolver `qk remote` uses
  (`QUEUEKEEPER_OFFLOAD_HOST`, else the alias `~/.ssh/config` defines), and asks it for its name and
  the keychain file over the same bounded SSH connection `qk doctor` makes, which never asks for
  a password. On the second Mac, where neither names a destination, its device record, which
  every `qk remote` run makes defer physical devices to the sending Mac, marks it as the target,
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

`qk keychain check` runs where the automation that uses the key runs: on the second Mac, through
`qk remote admit`, which runs it as every `qk remote` run is run, in its login shell under an
SSH login. It prints a JSON record with four separate results:

- `session`: the kind of session, `ssh`, `gui` or `background`, from the process's audit session
  flags, with a note on what a result there establishes. An SSH login is flagged remote and has no
  graphic access ([probing a session](https://github.com/vocaro/vocaro-engineering/blob/main/guides/background-mac-testing.md#probing-a-session)).
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
  result a GUI session.
- An unlock made beforehand does not reach an `qk remote` run. On the second Mac, the next run's
  check read the login keychain `locked` and signing failed with `errSecInternalComponent`, the
  identity present, both after the owner unlocked the keychain in its GUI session with Keychain
  Access and after an accepted `security unlock-keychain` in an `ssh -t` session from the
  workstation, which then ended (2026-10-29, macOS 27.2,
  [vocaro-engineering#65](https://github.com/vocaro/vocaro-engineering/issues/65)). So each run
  unlocks the second Mac's login keychain itself ([below](#keeping-the-second-macs-keychain-unlocked)).
  After any change to how a key is reached, run the check, and retry signing work only once it
  passes where the work runs.
- Keychain access control and partition lists (`security set-key-partition-list`), trust
  settings, certificates, TCC and host configuration stay the owner's decisions, and so does where
  credentials are kept, apart from the second Mac's login password, below. Neither `recover` nor
  `check` changes any of them; a failed check is reported for that decision.
- Not measured: the second Mac's own `recover` command from a shell on the second Mac.

### Keeping the second Mac's keychain unlocked

The owner keeps the second Mac's login keychain unlocked for its automation: the second Mac holds
no sensitive data and stays in a secure place (the owner's decision, 2026-10-30, which replaces
that of 2026-10-29 to sign only in the owner's GUI session there). Since an unlock reaches only the
session that made it, and every `qk remote` connection is an SSH login of its own, no unlock made
once can serve the runs: each run unlocks the keychain itself, in its own session, with the second
Mac's login password, which the owner gives once.

```sh
python3 scripts/qk keychain keep-unlocked [--identity NAME]
```

The owner runs it in their own Terminal, on the Mac that sends work to the second Mac or in a shell
on the second Mac. It establishes the Mac and the target as `recover` does and refuses as
`recover` does, then, on the second Mac, through `ssh -t` and the alias from the sending Mac or
directly there:

- refuses, having asked for nothing, on any Mac but the one the sending Mac reached, without a
  terminal to ask at, or before `qk remote` has made its root, `~/Development/build-host`;
- asks for the login password at the second Mac's own prompt, which shows nothing typed, and
  tries it with `SecKeychainUnlock` on `~/Library/Keychains/login.keychain-db`, three times at most;
  a password that does not unlock it is not kept;
- keeps the one that does in `~/Development/build-host/keychain-password`, written whole and
  readable and writable only by the owner's account, beside `git-credentials`;
- sets that keychain to lock neither after a time nor on sleep (`security set-keychain-settings`
  with no options), so that the owner's own sessions there keep it unlocked too, and prints its
  settings as `show-keychain-info` reads them;
- prints the check to run next. It exits 0 when it kept the password and set the settings, 1 when
  no password unlocked the keychain, which changes nothing, or the settings were not set, and 2
  when it refused.

Each `qk remote run` and `qk remote admit` then reads the file on the second Mac and unlocks the
login keychain with `SecKeychainUnlock` before it claims a checkout, in the run's own session,
which the work it starts shares; the run says so in one line. The password goes to Security only:
never into a command's arguments, an environment variable, a log, a receipt or chat. Without the
file a run unlocks nothing, as before. A file whose password no longer unlocks the keychain, such
as after the second Mac's login password changed, refuses the run before anything of it starts,
with the fix: run `keep-unlocked` again. The refusal is configuration, so the work does not move to
this Mac.

Measured on the second Mac (2026-10-30, macOS 27.2 26B123), after the owner ran `keep-unlocked`
from the workstation: in the next `qk remote admit` run, the run unlocked the login keychain, and
`qk keychain check` in that run's SSH session read it `unlocked` (`show-keychain-info` answered
`no-timeout`), found the Apple Development identity (1 valid) and signed with it: outcome `passed`,
with no partition-list or access-control change. A plain `ssh build-host` session a minute later
still read it locked ("User interaction is not allowed").

So it reaches `qk remote`'s runs only: a plain `ssh build-host` session, or a process started in
one, still finds the login keychain locked. To undo it, delete the file on the second Mac, and set
the lock settings the owner wants with `security set-keychain-settings`.

### Earlier names

Until 2026-10-28 the second Mac was the build host, which read as the one Mac that builds,
though both do. Its configuration names became offload names, and since
[vocaro-engineering#58](https://github.com/vocaro/vocaro-engineering/issues/58) queuekeeper reads none of
the old ones. It refuses each, naming what replaces it, rather than ignoring it: a checkout or a
shell that still uses one meant something by it, and ignoring it would move that work silently.

- `"buildHost"` on a lane or group, true or false: configuration validation refuses it, naming
  every lane and group that gives it. Delete it, since every heavy or exclusive lane goes to the
  second Mac unless it says why it stays, with `here`; a checkout older than its project's change
  rebases onto main.
- `qk run --build-host-only`: refused, beside any other flag too; use `--offload-only`.
- `devices recover --build-host-unchecked REASON`: refused; use `--offload-unchecked REASON`.
- `VOCARO_BUILD_HOST`, set to anything, even beside `QUEUEKEEPER_OFFLOAD_HOST`: refused; set
  `QUEUEKEEPER_OFFLOAD_HOST` instead, to the same value. Ignored, it could send work to the second Mac
  that it was meant to keep here, so queuekeeper refuses it wherever it reads `QUEUEKEEPER_OFFLOAD_HOST`: before
  `qk run` or `qk admit` chooses a Mac, in `qk remote run`, `admit` or `setup`
  without `--host`, and in `qk doctor`. A command that neither chooses a Mac nor reaches the second Mac, such as a `self`
  lane, a lane that says `here`, `qk run --here` or `qk spelling`, never read it and runs as
  before, so the second Mac's own `qk run --here` does not stop for it. Every other lane, a
  light one included, chooses a Mac, and is refused while it is set.

Nothing reads the other renamed words, so nothing refuses them: a receipt's `ranOn`, now
`second Mac`, and a device record's recovery, which records its check of the second Mac as
`offload`. Receipts and records written before keep `build host` and `buildHost`, since they are
data already on disk, and such a receipt still counts: `qk remote passed` and `eng release` read
the second Mac's receipts by commit, whatever their `ranOn`.

These keep the build host's name:

- The SSH alias `build-host`, which the owner defines in `~/.ssh/config`, stays the default, since
  the toolkit cannot change that file. queuekeeper also takes `second-mac`, as `Host second-mac` in place
  of `Host build-host` or both on one line, and without `--host` or `QUEUEKEEPER_OFFLOAD_HOST` uses the
  first of the two that `ssh -G` resolves.
- `~/Development/build-host` on the second Mac, which holds its bare repositories, its warm
  checkouts, the runtime staging and `git-credentials`: another name would make every checkout
  cold and leave the token behind.
- `refs/queuekeeper/hosts/`, under which a push carries a commit into those repositories: another
  prefix would orphan the refs already there, such as one a lost run left.
- Records made before the rename, measurements, logs and receipts among them, and the guides'
  dated observations, which keep their words.

## Reclaiming disk space

The rules above slow growth; they do not recover what crashed or finished sessions left behind.
Reclaiming that needs proof that nobody's live work is in it, and a quiet session, a passed
deadline or a finished-sounding name proves nothing. `qk disk` takes the inventory and changes
nothing:

```sh
python3 scripts/qk disk
```

It covers every project in `~/Development`, whichever project runs it, and reads only what Queuekeeper
and its plugins create on the host: the simulators and runtimes their tests use, the DerivedData and SwiftPM
caches their builds fill, the lane results Queuekeeper keeps by commit in their Git directories, and kept app
builds. It never surveys the home directory or the rest of the volume. It prints each item's size, the reason
for its status and the exact command that would remove it. What agent sessions leave is the toolkit's
`eng disk` to list and remove, by the same rules of proof: linked worktrees, scratch builds and results under
`/private/tmp`, kept failure records and evidence archives ([reclaiming disk space](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#reclaiming-disk-space)).
Run both when the volume runs short.
A **candidate** is proven unowned by the rules below, and `--delete` removes it without asking
(the owner's decision, 2026-11-05). An **owner** item shows no sign of a session using it, but
only the owner can say whether it is still wanted: test results, a directory no repository has
registered and a worktree outside the session directories. Simulators and runtimes are never
owner items: Xcode makes its simulators again and downloads its runtimes again (the owner's
decision, 2026-11-03, the first to let queuekeeper delete without asking). Everything else is **kept**:
in use, owned, of unclear ownership, or holding work or evidence. `qk disk --dry-run` lists only
the candidates, with their commands: exactly what `--delete` removes.

- **In use.** One `lsof` and `ps` census of the account's processes is taken first. A path is in
  use when a process's working directory, an open file or a path in its arguments lies inside
  it, and a path in use is kept.
- **Simulators**, from `simctl list -j`. A member of a pool that a main checkout's
  queuekeeper.json names in `simulatorPool` is a candidate only under the
  [pruning rule](simulator-pool.md#pruning), and its command is `qk simulator-pool prune`,
  which reserves it, checks it again and records the deletion. A device left by the retired
  [disposable lifecycle](https://github.com/vocaro/vocaro-engineering/blob/main/guides/simulator-testing.md#disposable-simulators), whose unique name carries a
  UUID, is a candidate when it is shut down, unused for the idle time, absent from every
  process's arguments and files, and unnamed, by UDID or name, in every device record: the one
  the devices module reads, the account's record when `QUEUEKEEPER_DEVICE_RECORD` names another, and
  any legacy `/private/tmp` handoff file. Every device is shut down and unused by any process
  before it can be a candidate, and an unreadable record keeps every device. Xcode's default
  devices, unavailable named devices and device directories CoreSimulator no longer lists are
  candidates, and `qk doctor` deletes this Mac's defaults itself; any other named device, which
  may be another pool's member on a checkout queuekeeper cannot see, is kept. A runtime no device uses
  is a candidate once simctl's `lastUsedAt` is over 14 days old, or absent, so a pool that will
  create its members on it next keeps it meanwhile; one that came with Xcode is kept.
  CoreSimulator resolves a runtime identifier's devices to one build, so when several builds of
  an identifier are installed, the devices count only for the chosen one: the build
  `xcrun simctl runtime match list -j` gives as `chosenRuntimeBuild` for an installed SDK, else
  the build whose image CoreSimulator used last. Any other build of the identifier is superseded
  and a candidate once its own `lastUsedAt` is over 14 days old or absent, unless the toolkit
  pins it (the builds qualified for
  [guarded VoiceOver activation](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md), `voiceover/activation.py`'s
  `QUALIFIED`) or a device record names it, for instance as a pool template's `runtimeBuild`. A
  template's `installedBuilds`, the builds installed when it was warmed, pins none: removing one
  only makes the pool warm that template again. An
  unreadable record keeps every superseded build, and an identifier whose choice neither the SDK
  nor a single last use shows has no superseded build. A pinned build is kept even with no
  device ([#188](https://github.com/vocaro/vocaro-engineering/issues/188)). With a second Mac, a
  runtime candidate's reason names the second Mac's copy, read as `qk remote setup --runtimes`
  reads it. The two Macs keep the same runtimes
  ([#123](https://github.com/vocaro/vocaro-engineering/issues/123)): that command deletes a runtime
  only one Mac lists when no simulator uses it there, as none uses a superseded build nothing
  pins, and otherwise copies it to the other Mac. So a runtime stays kept here while the second Mac holds a copy its simulators
  count as used, or while its runtimes cannot be read; removing it would only bring it back.
- **Build caches.** A DerivedData folder in `~/Library/Developer/Xcode` (named
  `NAME-<28 letters>`) whose `info.plist` names a workspace in `~/Development` or a temporary
  directory is a candidate when that workspace no longer exists; a folder for any other
  workspace is not the projects' and is not listed. One without `info.plist` can be traced to no
  project. When it holds nothing but `Logs`, `SourcePackages` and `TestResults`, which an
  `xcodebuild` without `-derivedDataPath` leaves when it resolves packages or tests without
  building, it has no build products to lose and is a candidate under the same rules: no
  process and idle for `--min-idle-hours`. So is one whose `Build` holds only `ProfileData`, the
  coverage data a `test-without-building` without `-derivedDataPath` leaves on every run: 1,850
  such folders, 15 GiB, on 2026-11-04, before `qk simulator-tests` gave each test step a folder of
  its own ([#158](https://github.com/vocaro/vocaro-engineering/issues/158)). One holding other
  `Build` contents or anything else is an owner item. The folders every build shares (`ModuleCache.noindex`, `SDKExplicitPrecompiledModules`)
  are kept. A clone or artifact in SwiftPM's
  cache, `~/Library/Caches/org.swift.swiftpm`, is a candidate too: SwiftPM fetches it again.
- **Kept lane results**, beside the receipts in each main checkout's
  `.git/queuekeeper/remote/COMMIT/LANE.results/`
  ([a lane's results](parallel-tests.md#a-lanes-results)), counted by lane. Their rule keeps a
  lane's results at its newest 10 commits, by the time its receipt was kept, and at every commit
  kept in the last 14 days, and removes the rest; it never removes a receipt, which is small and
  which eng release, a checkpoint and `qk remote passed` read, so a receipt's `keptResults` can
  name a folder that is gone. A landing gate's iOS shards keep about 1 MB at each commit, so the
  store would otherwise grow by that with every gated commit
  ([#218](https://github.com/vocaro/vocaro-engineering/issues/218)). An open issue or evidence
  that cites a commit's times does not keep them: evidence copies what it cites
  ([evidence](https://github.com/vocaro/vocaro-engineering/blob/main/guides/evidence.md)). A repository with results past the rule is a candidate, sized by
  them, whose command is `qk remote prune-results CHECKOUT` (`--dry-run` lists them); one with
  none past it, or whose store a process uses, is kept.

`qk disk --delete` removes every candidate category at once, on a terminal or not, without
asking: any session runs it when the volume runs short (the owner's decision, 2026-11-05; until
then it asked him on a terminal for every category but simulators and runtimes, and
`--yes 'owner, 2026-11-04 chat'` carried an approval he gave in chat into a run without one).
`--yes 'WHO ASKED'` now only names, in each ledger entry, who asked for the run, and without
`--delete` it is refused. Owner items are never removed, and
their commands are only printed. Immediately before each
removal it takes a fresh census and checks that candidate again, refuses an `rm` inside any
working tree or reached through a link, and appends every decision to
`~/Library/Application Support/queuekeeper/disk-reclaimed.jsonl`. It never runs an owner
item's command. Inventory and dry-run sizes are allocated sizes from `du`. APFS clones share
blocks, so removing one can free less than its size. To report how much space a cleanup freed,
measure free space on the same volume immediately before and after the run, for example with
`df -k /System/Volumes/Data`. Report the difference as the net change in free space and label
the dry-run footprint as allocated size. If you also measure each removal, label those changes
separately because other activity on the volume can affect the net change across the run.
Run the inventory again after `eng disk` removes worktrees, because DerivedData whose workspace went
with them becomes a candidate.

### Processes left over from runs

Those fills began the evening `qk remote` work started on the build host, came every six to
seven hours, and stopped at the restart that installed macOS 27.2 (26B123), at 02:05 UTC on
2026-10-27. The restart also emptied `$TMPDIR`, and with it what could have named the writer.
Nothing in the toolkit was found to write the space, and the investigation closed it as a likely
defect of the macOS 27.2 or Xcode 27.2 beta that the restart replaced
([vocaro-engineering#45](https://github.com/vocaro/vocaro-engineering/issues/45)). The other
explanation it weighed was a process that a run left running until the restart, and supervision
cannot see a child that moved to a session of its own and was orphaned between two samples
([process supervision](#process-supervision)). So the second Mac is searched for such processes
before work is sent there: `qk remote`, and `qk run` for a lane it sends there, print what the
search finds in the connection that prepares the second Mac's repositories, before anything is
pushed, and `qk doctor` lists it under `offload`, `leftovers`. A process is listed when it is
the account's, runs from a checkout there (`~/Development/build-host/checkouts`), the
temporary directory (`$TMPDIR`) or a simulator's device directory, and no live run accounts for
it:

- A process runs from a place when its working directory, as `lsof` reports it, lies in it, or
  its command line names a path in it. A program under `/System` is macOS's own:
  IMTransferAgent works in the temporary directory on both Macs.
- A live run accounts for the process when the process or an ancestor is a live admission
  owner, as the guard or the [admission log](#admission-log) records it, or a live
  `offload_host.py`, which writes its PID and start into the checkout slot's lock file when it
  takes the slot, for the moments between the run's admissions, such as its checkout's placement. A recorded
  PID counts only while its process has the recorded start, and an admission that the log has
  not ended counts only for a process that started before it.
- Some of a run's processes launchd starts or adopts, and what a live run holds is its own with
  everything below it. A simulator's processes descend from `launchd_sim`, never from a run, so
  a device is a live run's while the second Mac's [device record](#device-reservations) holds
  it, unreleased, for one of that run's processes with its recorded start, or while one of them
  names its UDID. The reservation is the exact signal: `qk simulator-tests` names its devices
  by their kind, so the run names a device only while an `xcodebuild` of its names it. A pool
  member's reservation records the process that took it (`holderProcess`), and an active run's
  records its process.
- An Xcode program, such as the `DTServiceHub` that `xcodebuild` starts and leaves to launchd
  once it ends, is a live run's when it runs from a checkout slot in which a live admission's
  owner works, and started no earlier than that admission, by the log's time or else the owner's
  start. So is the executable of an app, XPC service or app extension (`.app`, `.xpc` or `.appex`
  `Contents/MacOS/`) built in that slot, which launchd starts when a UI test launches the app:
  on 2026-11-03 the census listed a live Pagepour `mac-test-suites` run's app and its converter
  service as left over (vocaro-engineering#129). That window is the only signal they leave, so
  other programs from the checkout, a bundle's scripts among them, are still listed.
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

The search itself only reports. A search that fails says why, and the work is sent all the same.
Tell the owner what it lists, with its PIDs and their writes: whether to stop a process is the
owner's decision, as is any other change to a shared host
([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/agents/core.md#shared-resources)). The exceptions are a process that an
ended run provably launched, and a stale pool simulator, both below.

### Leftovers an ended run launched

The owner wants routine cleanup to stop a process the toolkit can prove an ended run left
([vocaro-engineering#353](https://github.com/vocaro/vocaro-engineering/issues/353)). On 2026-11-10
two EPUBLib dispatches each listed a BreezEPUB Preview app extension, parent 1, running from a
checkout slot with no live run accounting for it, and left it running: a path, parent 1 and the
census's silence say only that nothing claims a process now, not that a run started it, and an
extension from a slot's build products also starts when anything on the Mac previews a file.

So each dispatch records, while it holds its checkout slot, the processes launchd starts or adopts
from the slot's bundle executables after its claim: `offload_host.Launches`, sampled every
`LAUNCH_SAMPLE_SECONDS` and once as each command ends, in `SLOT.launched.jsonl` beside the slot's
lock, each line naming the process by PID, start and command line and the run by its PID and
start. The cleanup that reaps stale pool simulators, before work is sent to the second Mac and in
`qk simulator-pool reap` there, then runs `offload_host.stop_leftovers`; `qk doctor`, a diagnostic,
runs it as a dry run, which signals nothing and lists each process it would stop (`would stop PID
…`). Its `proofs` lists every condition a process must meet; it stops nothing while the census
could not check everything. It runs the census and every check again from fresh listings immediately before
the signal, sends SIGTERM alone, and logs each stop with why it qualified in
`leftover-stops.jsonl` beside the admission log, before the signal and after it ends. Each leftover
it keeps is said with the evidence that is missing (`did not stop PID …: …`), and stays for the
owner as above. A process started by hand, by a run that was not watching, or before this
recording existed has no record, and is only listed; so are Xcode's programs, such as
`DTServiceHub`, and anything in the temporary directory or a simulator's device.

### Stale pool simulators

On 2026-10-31 a `Pagepour Duo 1` simulator, a `launchd_sim` with about 320 descendants, sat booted
on the second Mac for 22 hours waiting for that decision. The owner has decided it for this one
case: any session may run `python3 scripts/qk simulator-pool reap`, without asking, and queuekeeper runs it
in the connection that lists leftovers before work is sent to the second Mac, and on both Macs in
`qk doctor` (the owner's decision, 2026-11-03). Heavy work that runs on the Mac it was started on, a lane
`qk run` runs there or a command `qk admit` runs there, reaps that Mac the same way once it is admitted and
before it starts, so that a run here frees what is left over before it takes simulators
([vocaro-engineering#156](https://github.com/vocaro/vocaro-engineering/issues/156)). Work a dispatch sent
to the second Mac does not reap again, since its dispatch just did, nor does work nested in an admission
it inherits, nor a `self` lane, whose `qk admit` commands reap each as it is admitted. It runs
`xcrun simctl shutdown UDID` on a simulator only when all of these hold:

1. It is a member of a project's pool: named `POOL LABEL N`, for a pool named by the project, or by a
   project's main checkout here or its checkout on the second Mac. Or it is that pool's warmed template,
   `POOL LABEL template (RUNTIME)` on the runtime its name gives, as `qk simulator-pool list` shows one
   ([warmed templates](simulator-pool.md#warmed-templates)). Asked on 2026-11-04 "May the pool's reap
   shut down a warmed template a killed warm-up left booted?", the owner answered "Yes, reap them"
   ([vocaro-engineering#153](https://github.com/vocaro/vocaro-engineering/issues/153)). The stop is
   logged and printed as a warmed template's, not a member's. Before booting a newly created template,
   the toolkit records its pool, name, runtime, UDID and creator's process identity beside the simulator's
   data directory. Unscoped reap can recognize that exact template after its checkout or isolated test
   record is gone. It keeps a template while its recorded creator still runs, comparing PID and start.
   A template-shaped name alone supplies no ownership evidence for an unknown pool (#336).
2. The search above lists its `launchd_sim` as left over, and no process of the account outside that
   simulator's own names its UDID.
3. The device record holds no live reservation of it, by its name or its UDID. A reservation whose
   recorded process (`holderProcess`, an active run's process, `--own` processes) is gone, or whose
   PID now has another start, is not live, and once the simulator is shut down reap releases its
   entry, as below. One that records no process, is marked uncertain, or has not reached its expected
   release counts as live, except a run's reservation (`heldOnlyWhileHolderRuns`) whose recorded
   processes are all gone and whose owner nothing accounts for, by the census of the early release
   below (the simulator's own `launchd_sim` and its descendants, which name its UDID, left out): that one
   is not live before its expected release either, so reap shuts the simulator down and releases its
   entry in the same run. Asked on 2026-11-05 whether reap may shut down a booted pool simulator whose
   reservation would qualify for early release, the owner answered yes
   ([vocaro-engineering#156](https://github.com/vocaro/vocaro-engineering/issues/156)). A take's
   reservation, or one with a live holder or a live admission or process naming its owner, keeps the
   simulator running until its release is due.
4. Its `launchd_sim` has run for 2 hours (`offload_host.REAP_AFTER`), and all of this is still so at
   a second check immediately before the stop.

**A pool member with no live reservation goes after 2 hours, whoever booted it.** That includes one the
owner booted by hand. To keep one running, boot a simulator outside the pool, or reserve it with
`python3 -m queuekeeper.apple.devices reserve`, which records no process and so counts as live. A
session that took a member with `qk simulator-pool take`, whose process ends when it prints the
UDID, holds a reservation whose process is gone, which counts as live until its expected release,
an hour after the take unless `take --hold MINUTES` asked for longer. To work past that, it runs
`qk simulator-pool extend UDID [--hold MINUTES]` (an hour from now by default), or records a running
process of its own (`devices update KEY --own PID`) (vocaro-engineering#179).

**A template stays while its warm-up runs.** The pool reserves a template in the warming process's name,
its `holderProcess`, before it boots it, so a live warm-up's reservation keeps it running however long
the warm-up takes. A warm-up killed outright (`SIGKILL`, or the Mac stopping) leaves the template booted
and its entry held by a process that is gone. Its reservation is a run's (`heldOnlyWhileHolderRuns`), so
once the template has run 2 hours and nothing accounts for its owner, reap shuts it down and releases its
entry, as below, before its expected release, 8 hours after the reservation.
Until the entry is released, the kind's new members are created rather than cloned, and once it is
released the next creation replaces the template, which was never warmed to the end.

It erases, deletes and kills nothing, and other leftovers, such as `DTServiceHub`, stay report-only.
It stops only what it is sure of: a census with anything unread, an unreadable device record or
admission guard, or a `simctl` that does not list its devices stops nothing and says so. A failed shutdown is
reported and not tried again in that run; the next run may try it once more. Each stop
is printed, for the session to report, and logged in the Mac's own
`~/Library/Application Support/queuekeeper/simulator-reaps.jsonl` as a `stopping` line written
first, then `stopped` or `failed`, with its UDID, name, `launchd_sim` PID and start, age in seconds, and
reason. A log that cannot be written stops nothing. The log is a sibling of the
[admission log](#admission-log), whose audit reads only admissions.

With no options the command acts on this Mac and then, over the search's connection, on the second
Mac. `--dry-run` only says what it would stop and what it keeps, and why; `--here` leaves out the
second Mac; `--pool NAME` looks at that pool alone. It exits 1 when anything failed, a second Mac that
cannot be reached among them. Before a dispatch or a run here it is bounded: one reap spends at most 60
seconds (`REAP_SECONDS`), starts no stop with under 20 left, and leaves the rest for the next one; every
problem is printed and the work is sent, or runs, all the same. With nothing listed it adds only `ps` and `lsof`.

A reap cannot race a run that takes the device, by three things. `Pool.take` only takes a member that
`simctl` shows shut down, and reserves it, under the record's lock, before it erases or boots it, so a
reap never finds a take's fresh boot unreserved. A stale simulator has run for 2 hours, which a booting
member has not, and `take` passes over a booted one. And the second check runs holding the device
record's lock, which every reservation takes, until the shutdown has started, so no reservation lands
between the check and the stop. It holds that lock for at most 20 seconds, under the 30 another change
waits, and gives up with nothing stopped when it cannot have the lock in 5.

**Reap releases a pool reservation it proves dead**, on whichever Mac the entry is on, so that no
session recovers leftover simulators by hand or brings them to the owner (the owner's decision,
2026-11-04, [vocaro-engineering#156](https://github.com/vocaro/vocaro-engineering/issues/156), which
reverses the report-only rule of [#97](https://github.com/vocaro/vocaro-engineering/issues/97)). Pool
members are erased before each take, so a dead holder leaves nothing on the device to restore. Each run of
`qk simulator-pool reap`, the reap before a dispatch to the second Mac or a run here and `qk doctor`'s releases an
entry of that Mac's device record only when all of these hold, read in that order:

1. The entry records a process (`holderProcess`, an active run's process, `--own` processes) and every
   one of them is gone: its PID is not in `ps`, or runs with another start. `ps` is read after the
   record, so a process it lacks was recorded before it went, and the listing must show the reap's own
   process, or it shows nothing gone. An entry that records no process, or a running PID with no start
   to compare, is left.
2. Its expected release has passed, or nothing accounts for its owner (the owner's decision,
   2026-11-04, in chat, taking #156's alternative). Before its expected release an entry goes only when:
   - it records `heldOnlyWhileHolderRuns`, which a run's own pool writes beside `holderProcess`
     (`qk simulator-tests`, `simulators.pool.member`, a warm-up or prune) and `qk simulator-pool take`
     never does. A take's process ends when it prints the UDID, and its session then holds the member
     by its word; a session between commands runs no process that names it (an agent's own process
     carries no session ID, only the commands it starts do), so no census can show that it has gone,
     and a take is never released early;
   - no live admission, in the guard or the admission log, has a process, itself or a descendant, whose
     command line or environment names the owner (`ownerChatID`), and no other process of the account
     names the owner or the device's UDID, in a `ps -axwwE` listing read after the record. The listing
     must show the reap's own environment, and an owner shorter than 8 characters cannot be searched
     for; either leaves the entry held. An admission record that cannot be read leaves it held too.
3. It is not uncertain or `restoration-unverified`, and its run did not go to another Mac (`run.host`),
   whose processes this Mac cannot check.
4. It is a simulator pool's: its device is a simulator, `simctl` lists that UDID under the entry's key,
   and the key is a member or warmed template of a pool reap looks at.
5. `simctl` shows the device `Shutdown`, after this reap stopped it if it was stale, and no other held
   entry names the device.
6. The reap met no problem, and under the device record's lock the entry is still at the revision it read.

It releases the entry as `devices recover` does, at the next revision, with `recovery.by` set to `reap`,
the condition (`held`, or `orphan` for an active run whose process is gone), `recovery.qualified` (`its
release passed`, or `early: nothing accounts for its owner`) and `recovery.verified` naming the PIDs, their
starts, the release that had passed or, for an early one, what the owner's census checked, and the
`Shutdown` it saw; it logs a `releasing` line, with the same `qualified`,
before it writes the record, then `released`, in `simulator-reaps.jsonl`, so a log that cannot be written
releases nothing. A released reservation that `qk simulator-pool take` made also gets a `reap` line in
`simulator-holds.jsonl` (simulator-testing.md, Reserved simulator pool), which, unlike these, never holds
the release up. Each release is printed (`released KEY (UDID), held for owner …`); `--dry-run` says
`would release` and changes nothing.

An entry whose processes are gone and whose release has passed, but which reap could not prove dead by the
rest, stays held and is named with why (`reap did not release it: simctl shows it Booted, not Shutdown`,
for one; its simulator is still under 2 hours old) and its exact `devices recover` command; for an entry
in the second Mac's record that command runs there, as `ssh HOST 'cd
~/Development/queuekeeper/src/python && python3 -m queuekeeper.apple.devices recover …'`, since
run here it would read this Mac's record. `qk simulator-pool reap`, with or without `--dry-run`, and
`qk doctor`'s `reaped` name every such entry; the reap before a dispatch or a run here names only those of
simulators it just stopped. `devices status` names them too, and releases nothing. One not yet due that
reap leaves (`its release is due …; reap did not release it: …`, a take's or one whose owner something
accounts for) is named only by `qk simulator-pool reap`. A member still booted whose dead reservation
is not yet due but qualifies early, a run's whose owner nothing accounts for, reap shuts down once it has
run 2 hours and then releases in the same run (the owner's decision, 2026-11-05); `--dry-run` names it as
one it would stop and would release. A take's, or one whose owner something accounts for, stays booted
and held until its release is due. A live holder, a
release that cannot be read, an entry that records no process and an uncertain entry are neither named
nor released.

### Registered Mac builds

Xcode registers a Mac app when it builds it, and macOS registers one when it launches, and neither is
undone. On 2026-10-31 the second Mac had 22 registered builds of one app, whose Quick Look extensions
shared their bundle identifiers and version; the system ran an old copy's extension, and a new build's
never ran ([vocaro-engineering#87](https://github.com/vocaro/vocaro-engineering/issues/87)). The same
holds for document types, URL schemes, Spotlight importers, services and Share extensions.

So when an `qk run` or `qk admit` ends, by a pass, a failure, its time limit or a stop, queuekeeper runs
`pluginkit -r` on each `Contents/PlugIns/*.appex` and `lsregister -u` on each Mac app under the derived
data its xcodebuilds named (`app_registrations`). A lane needs no option: queuekeeper reads the `-derivedDataPath`,
or the folder of `-xctestrun …/Build/Products/…`, of each xcodebuild among the run's descendants, however
deep in its scripts, every 0.5 seconds. Nested runs leave this to the outermost, so a run keeps its
registrations until it ends.

- **What counts as built.** Whatever lies in a folder the run's own xcodebuild named, up to date or not:
  launching an app that an incremental build or `test-without-building` left unchanged registers it again.
  Give each lane its own folder, as parallel runs need anyway.
- **What is never touched.** Anything outside the folder, through a symbolic link included; an iOS app,
  which has no `Contents`; a folder another live run names, or whose path a process outside this run's
  lineage names, which waits; and a path that cannot be derived data: one holding the home folder or the
  run's directory, a checkout, or Xcode's default folder, which every session shares.
- **SIGKILL.** It runs no cleanup. The run's journal, in `app-registrations` beside the admission log,
  stays and its builds stay registered, until the next outermost run on that Mac, in any project, finds
  that the journal's PID is gone or has another start, and sweeps it. Until that run, the system can still
  pick the killed run's extension. A folder that had to wait, or a tool that failed, is swept the same way.
- **Not covered.** An xcodebuild with no `-derivedDataPath` (the run says so); a copy of a build outside
  its folder, such as one in an evidence folder; a folder removed before the run ends, since nothing is
  left to name to `lsregister`; an xcodebuild that a script starts and ends between two samples; and
  registrations from before this existed, in a folder no later run uses.

A run says what its sweep and cleanup did in one line on stderr, a count of what it unregistered and of the
folders it left registered, whose reasons are in the file it names, each folder once. A tool's report that the
system holds no such registration (lsregister's `-10814`, pluginkit's `no plugin at`) counts as done: its journal
goes. A problem here is printed on stderr and never changes the run's result.

## Device reservations

Physical devices, and the members of a project's simulator pool, are
used only under a real reservation in the host's one device record, a human-readable JSON file
outside every repository: `~/Library/Application Support/queuekeeper/device-handoff.json`
of the account the sessions run as. `src/python/queuekeeper/apple/devices.py` is its only
writer. It finds the record from the account's home directory, not `HOME` or `TMPDIR`, so a
session with a private environment joins the same record; `QUEUEKEEPER_DEVICE_RECORD` names an
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
export PYTHONPATH="$QUEUEKEEPER_ROOT/src/python"
python3 -m queuekeeper.apple.devices status
python3 -m queuekeeper.apple.devices show iPhone    # the entry and its revision; 0 if new
python3 -m queuekeeper.apple.devices reserve iPhone --owner SESSION --revision N \
  --device UDID --purpose "…" --release-by 2026-10-21T18:00:00Z \
  --set 'originalSettings={"voiceoverEnabled": false}' --set 'preservation=…'
python3 -m queuekeeper.apple.devices update iPhone --owner SESSION --revision N --set 'run=…'
python3 -m queuekeeper.apple.devices release iPhone --owner SESSION --revision N \
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
never releases a device; `status` reports an overdue entry and changes nothing. The one exception is
a simulator pool's reservation whose processes are all gone and whose simulator is shut down, past its
expected release or, for a run's reservation, before it once nothing accounts for its owner, which reap
releases as a recovery
([stale pool simulators](#stale-pool-simulators)). Release yours
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
you own or have reserved, never a name or `booted`. [Simulator testing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/simulator-testing.md)
covers the pool's take, use and release steps.

A physical device's reservation records the owner's approval, and approval of a run covers
installing and launching there, not operating the device by hand. [Device testing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/device-testing.md)
has the preflight, UI-automation authorization and restoration steps for physical devices.

### Taking the iPad or iPhone in turn

The record schedules the paired iPad and iPhone, first come first served, one holder at a time,
and records no approval (owner's decision, 2026-11-06). Whether a session may use a device is
between the session and the owner: the iPad carries his standing approval (2026-10-31), so a
session takes it whenever it is free, without asking; the iPhone is his personal phone
(2026-11-01), so a session asks him first and takes it only once he has agreed, with
`--release-by` at the end of the time he gave. Both are taken the same way:

```sh
python3 -m queuekeeper.apple.devices take iPad --purpose "…" --release-by 2026-10-31T09:00:00Z \
  --set 'originalSettings={"voiceoverEnabled": false}' --set 'preservation=…'
```

`take` joins the entry's line, waits while another session holds the device or is ahead of it,
saying who holds it and until when, and reserves it until `--release-by`.
It prints the entry, whose revision the session's next change names. Start it in the background
when the wait may outlast the tool's; `--wait SECONDS`, an hour by default, bounds it, after which
it leaves the line and reserves nothing. The line is kept beside the entries, in the record's
`queues`, so joining it never changes the holder's revision. Each place in it records the waiting
`take` process and its start time, and a place whose process is gone drops out. While anyone waits,
`reserve` refuses everyone but the first in line.
`take` refuses an entry that has never recorded its device, which its first `reserve --device`
records, and an orphaned or uncertain entry, which needs attended recovery. Nothing hurries the holder: an overdue expected release still releases
nothing, so a wait behind a session that never releases ends at `--wait`, and `status` shows the
holder and the line.

Reserving or taking a physical device also silences the owner's notifications on it for the
run, with the shortcuts he made there once, and releasing or recovering it puts them back; the
entry's `notifications` records each step. Without those shortcuts the command says what to ask
the owner when asking for the device. `devices run` watches the device's log for a
banner while its command runs, and runs a command that one reached once more
([notifications during a run](devices.md#notifications-during-a-run)).

Record each app the session launches on the device with `devices update KEY --launched BUNDLE`.
Releasing an entry that records any requires `--apps-quit "…"`, saying how they were quit (for
example `devicectl device process terminate`), so the next holder does not find them running. The
Shortcuts app that `devices take` opens for the silencing shortcut is queuekeeper's own and needs no such
word. The next entry starts without them; its `previousLease` keeps them. An app running on a device a
session holds is not another session's work, since the record shows nobody else holds the device:
a leftover the holder may quit, but never uninstall or clear beyond the agreed cleanup.

### Device windows

A physical device's reservation records no approval (owner's decision, 2026-11-06): its window
is the reservation itself, until `--release-by`, and a bounded run stops before that. The record
refuses `--approved-*` on a physical device, a hardware UDID of 8 and 16 hex digits or 40, since
the owner's agreement is between the session and the owner. Only `mac-foreground` records the
owner's approval. A reservation made before then recorded
one, and work on it stops once that has ended: `devices.require_window(KEY, entry)` refuses,
`qk remote --device` refuses, and `status` reports the device's work as refused. An ended window
releases nothing; the session restores the device and releases it.

### Bounded device runs

Run a command on a physical device through the toolkit's one launcher for a bounded run:

```sh
python3 -m queuekeeper.apple.devices run iPhone --owner SESSION --revision N --limit 180 \
  --restoration 60 -- xcodebuild test-without-building … -destination "platform=iOS,id=UDID"
```

A project's launcher calls it from Python, `devices.BoundedRun([KEY], owner, command, revision=N,
limit=…, restoration=…).execute(command, stdout=log)`, and adds its own restoration queries and
release around it, as Pagepour's mobile UI session does. `qk remote --device` runs the same
checks and bound for a run on the second Mac ([below](#physical-devices-and-the-second-mac)).
The launcher:

- requires the session, `--owner` or else `QUEUEKEEPER_DEVICE_OWNER` or the agent session, to hold
  KEY with no run active, at `--revision` when given, and KEY to name a physical device. It never
  acquires, extends or changes a reservation;
- requires the window the entry records, as above, and refuses once it has ended
  (`devices.require_window`);
- refuses, running nothing, a command that names any device but the entry's: a hardware UDID in
  any case, though not a longer run of hex digits such as a SHA-256, or any device the record
  holds under another key, a simulator among them. Forty hex digits are an older device's UDID
  or a commit's whole SHA-1, so 40 that the current directory's Git knows as an object
  (`git cat-file -e`) name no device; a device's UDID never is one. Nor do 40 that name a
  code-signing identity's SHA-1 where a signing setting or option takes them: right after a
  name ending in `IDENTITY=`, such as `CODE_SIGN_IDENTITY=` or `SIGN_IDENTITY=`, after
  `--identity`, or after `-s` or `--sign` in a `codesign` command. It cannot recognize a
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
- treats a command that exits 0 as unmeasured, status 3, when its output reports a test count and
  no test ran, every one counted having skipped or none counted at all. On 2026-11-11 a
  `test-without-building` run whose `-only-testing:` named a Swift Testing suite printed
  `Executed 0 tests` and `** TEST EXECUTE SUCCEEDED **`, and exited 0 having measured nothing
  (vocaro/queuekeeper#100). `--no-tests` keeps status 0 for a run meant to select none. A command
  that prints no count, such as an install, keeps its status;
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

Every `qk remote` run first makes the second Mac's record defer physical devices to the sending
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
python3 scripts/qk remote admit --device iPhone -- COMMAND
python3 scripts/qk remote run --device iPhone LANE -- ARGUMENTS
```

- `--device KEY`, repeatable, makes the dispatch a [bounded run](#bounded-device-runs) of the
  reservations held here: the session holds each KEY here with no run active, within the owner's
  window, KEY names a physical device, and every device the command or the lane's arguments
  name is one of them; otherwise nothing is sent. `--device-owner` names the session, else
  `QUEUEKEEPER_DEVICE_OWNER`, else the agent session.
- The dispatch marks each entry's run active with the `qk remote` process as the run's process,
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
- The run reads its reservations from `QUEUEKEEPER_DEVICE_LEASE`:
  `{"record": MAC, "devices": {KEY: UDID}, "until": END}`, where END is the reservations' end.
  There `devices run KEY -- COMMAND` takes KEY from the lease, since the second Mac's record
  defers physical devices: it checks the devices the command names against the lease, bounds the
  command by END as here, and leaves the recording to the dispatch.
- The dispatch watches each leased device's log from this Mac while it lasts, and sends a run a
  notification banner reached once more (guides/devices.md, Notifications during a run).
  A `devices run` in the work watches from the second Mac as well, and reruns on its own.
- The device stays reserved after the run. Preflight, restoration and the release are the
  session's, from this Mac, which reaches the device too.
- Without `--device`, a command, or a lane's arguments, that names any hardware UDID is refused,
  whether or not this record holds its device, so a run on a device goes through a reservation:
  reserve the device here and add `--device KEY`. As in a bounded run, a simulator's UUID, a
  longer run of hex digits such as a SHA-256, and 40 hex digits that the Git of the project's
  checkout knows as an object, such as a commit's whole hash, or that a signing setting or
  option takes as a code-signing identity's SHA-1, name no device, with `--device` or
  without it. `qk run` keeps a lane whose arguments name a physical device of this record on
  this Mac, and `qk admit` a command that names any hardware UDID.

A dependency commit can be absent from the caller's Git object database. Declare it before the
command with `qk remote admit --git-commit /path/to/dependency=FULL_SHA -- COMMAND FULL_SHA`.
The repeatable option verifies the full 40-digit hash as a commit in that local repository before
sending anything. Only an exact, separate positional command argument is exempt; `id=FULL_SHA`, a hash following
an option, or a hash inside a shell command string still follows the device checks. A device known
to the record always needs its reservation. The command receives the original hash unchanged;
the dependency checkout path is used only for local verification and need not exist on the
second Mac. A missing object, tree, tag object, invalid hash, or unreadable repository refuses
the dispatch. Declare each foreign commit separately; this grants no exemption to other hashes.

`qk doctor` reports both Macs' records under `deviceRecord`. It is an error for this Mac's record
to defer physical devices, or for the second Mac's to hold one or to defer them to another Mac;
a record there that defers them to no Mac yet is listed as unchecked until the next run.

The records constrain only work that goes through the toolkit: `xcodebuild` or `devicectl` run
by hand on the second Mac, or Xcode there, reaches the devices without them. A lane that needs a
device says so with `"here"`, so sending one to the second Mac is always an explicit
`qk remote --device`.

The dispatch reports the time left and cleanup allowance before sending the run, then reports them
again when the work starts on the runner, alongside the admission wait. Checkout preparation also
consumes the window. The report warns if cleanup and the declared budget exceed the time left. It
uses an explicit `--time-limit` for an admitted command or an explicit `timeout` for a single lane.
The warning is advisory. The existing reservation bound still stops the run in time for cleanup.

## Unity editors

A project that lists the [Unity plugin](../src/python/queuekeeper/unity/README.md) runs its editor
unattended. This section says what several sessions on one Mac share, and what the plugin does
about it.

### What is separate for each project path

Unity's exclusions are per project path, and a Git worktree is a different path. So two worktrees
of one repository hold two separate Unity projects, and each may have its own editor open.

| State | Scope |
|---|---|
| The project lock, `Temp/UnityLockfile` | Per project path. A second editor on the same path is refused; editors on two worktrees are normal |
| `Library/`, `Temp/`, `Logs/` and `UserSettings/` | Per project path. Each worktree makes its own |
| The Pipeline package's server and token, in `Library/Pipeline/` | Per project path. Each editor takes its own port |

The cost is the cold `Library/`. A new worktree's first editor imports every asset and compiles
every shader, which takes minutes and several gigabytes. Keep a worktree and use it again, and let
`qk disk` reclaim the folders of the ones that are idle.

### What is shared

- **The GPU.** Every editor and batch run on a Mac uses one device. Results computed from state
  stay valid while runs overlap, but a time measured while another run uses the GPU is void. Run a
  measurement in an exclusive lane.
- **An editor in Play mode.** It keeps simulating while unfocused, and admission cannot see it. An
  exclusive `"unity": true` lane ends unmeasured while one plays on that Mac.
  `qk host editors` shows which.
- **The licensing helper.** One helper serves a user's editors. The last editor to close can leave
  it alive but unanswering, and the next editor then waits 60 seconds and fails. The plugin stops
  the helper when no editor is running, before it starts one.
- **A push.** A pre-push hook that runs the project's tests starts a batch editor. A push during
  another session's measurement loads the GPU as an open editor does, so ask that session to hold
  its pushes too.

### Running a batch while an editor is open

A batch run needs the project lock, which an open editor holds.

```bash
python3 scripts/qk unity closed -- ./tools/verification/check.sh --gates
```

This closes the project's editor with SIGTERM, waits for it to exit, runs the command, and opens
the editor again. It never force-kills. An editor that stays open, for a prompt about unsaved
changes, ends the run unmeasured with nothing run.

A lane that runs the editor says `"unity": true`. Queuekeeper then checks, on the Mac that runs the
lane, that a batch run can start. When it cannot, the lane ends unmeasured, and the merge queue
tries it again and then holds. No commit is sent back as failed for it.

### Rules the plugin keeps

- **Match an editor by its absolute project path.** A short fragment such as `mpm/lab` also matches
  the main checkout's editor from a worktree. That once closed another session's editor and opened
  it again on the wrong project (vocaro/mpm, 2026-08-15).
- **Judge the lock by who holds it open.** A closed editor leaves `Temp/UnityLockfile` behind. The
  plugin removes it only when `lsof` shows that no process holds it, and leaves it when `lsof`
  cannot answer or the path uses a symbolic link.
- **Use the editor the project pins.** A newer editor reimports the `Library/` and rewrites
  `ProjectSettings/ProjectVersion.txt`. `qk doctor` warns when a Mac lacks the pinned version.
- **End a silent run.** A batch run whose log has not been written for ten minutes is ended with
  its import workers, since a stuck run keeps the lock.
- **Never pass `-nographics`.** Without a graphics device Unity does not load compute shaders.

### A second session's checklist

1. Create the worktree. Open its editor with `python3 scripts/qk unity open --ensure` when it is
   needed, and expect a cold import the first time.
2. Run `python3 scripts/qk host editors` before driving any editor, to see which are open and
   which is in Play mode.
3. Keep at most one editor in Play mode.
4. Run measurements alone, in an exclusive lane.

## Attended Mac sessions

Mac VoiceOver and foreground UI default to the [second Mac's foreground](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md),
which queuekeeper reserves for each run without asking the owner, not to this Mac. These rules
are a method for a session on this Mac's foreground within a period the owner approved
beforehand ([this Mac's foreground](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md#this-macs-foreground)); they grant
nothing by themselves. The drivers' check below is the same on both Macs.

Agree the window with the owner first, then reserve the foreground for it before taking it.
The `mac-foreground` entry in the [device record](#device-reservations) stands for the screen,
keyboard, pointer, focus and speech, and names no device. Like a physical device's, its
reservation records no approval (owner's decision, 2026-11-06, vocaro-engineering#207): the
record schedules it, one holder at a time, and `--release-by` is the end of the window the owner
agreed. What the owner said is between the session and the owner, and `--approved-*` is refused.
A new window from the owner is a new reservation, after a release. Another session sees the
holder in `devices status`, and its reservation is refused while the entry is held.

```sh
python3 -m queuekeeper.apple.devices reserve mac-foreground --owner SESSION --revision N \
  --purpose "Mac XCUITest of …" --release-by 2026-10-23T18:00:00Z
export QUEUEKEEPER_DEVICE_OWNER=SESSION
python3 -m queuekeeper.apple.devices check      # at the start and before each action
python3 -m queuekeeper.apple.devices update mac-foreground --owner SESSION --revision N \
  --own PID
python3 -m queuekeeper.apple.devices release mac-foreground --owner SESSION --revision N \
  --outcome "…" --restoration "…" --frontmost restored
```

A foreground driver calls `devices check`, or `devices.require_foreground(owner)` from Python,
at the start and before each key, activation or `set frontmost`. It refuses unless the session
named by `--owner` or `QUEUEKEEPER_DEVICE_OWNER` holds the entry, the entry is neither orphaned nor
uncertain, and, on this Mac, its expected release has not passed. `qk run --here` checks a lane marked
`"foreground": true` the same way, and `qk admit --foreground` a command; on the second Mac queuekeeper
holds the entry for the run itself and names it in `QUEUEKEEPER_DEVICE_OWNER`
([running foreground work](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md#running-foreground-work)), and a run there may
outlast its expected release. An uncertain entry needs attended recovery, with one exception there:
when a session's own VoiceOver restoration could not be verified, and nothing else is uncertain, the
run's own runner may set VoiceOver back to the original queuekeeper recorded in the entry
(`voiceOverOriginal`) with `voiceover.state.recover_restoration(owner)`, once, while the run lives;
verified, the entry is no longer uncertain and the run's next session starts (owner's decision,
2026-11-12, [EPUBLib#416](https://github.com/vocaro/EPUBLib/issues/416);
[recovering the foreground](foreground.md#recovering-the-foreground)).
On this Mac a passed expected release releases nothing: `status`
reports it, the check refuses further foreground work, and the session stops, restores and
releases. A reservation made before 2026-11-06 that recorded an approval also stops once that
approval ends. Record each app and test runner the session launches
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

[Background Mac testing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/background-mac-testing.md) and [Mac VoiceOver](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-mac.md)
describe the drivers, key guards and VoiceOver restoration.

## Process supervision

Keep three deadlines distinct: the tool's output wait, the connection or launch timeout, and the
test's behavior or observation deadline. A tool timeout is an observation timeout. A tool that
returns a session handle may leave the command running: resume the handle and inspect its log
and processes rather than launching a duplicate. Wait for a confirmed terminal result before
starting dependent tests or pushes. Never infer termination from a missing parent or an expired
observation wait.

Supervise the process group you own. The toolkit's `processes.supervise`, which `qk run`,
`qk admit` and the resource CLI use (`qk run` with a unique log per lane under
`.build/queuekeeper/LANE/`), follows these rules:

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
  at the first sample that sees it, when a group or session is first seen without the command
  or used again, and the command's confirmed end. When the admitting process is killed before it
  settles its entry, `qk admissions recover` runs this census against the record.
- A push keeper ([checkout provisioning and publication](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#checkout-provisioning-and-publication))
  supervises its own commands and applies the same census to a command that fails on its own,
  with a positive status and no stop. When the census finds nothing, the keeper releases its
  heavy-work admission but keeps the checkout's transaction lock, its owner record showing
  `"failed": true` and `"childrenUncertain": false`, so the next push from that checkout is
  refused until someone has read why this one failed and removed the lock. A stop, a command
  ended by a signal, a killed keeper or a process the census finds leaves both in place, the
  admission entry marked uncertain, even after the observed children drain. A command that
  passes has no census there either. The keeper appends what it sees of each command it runs
  inside the admission to the admission's process record, as the supervisor does, so
  `qk admissions recover` checks a killed or uncertain keeper's entry as it checks any other;
  the checkout lock stays for a check by hand.
- Once the root exits, its observed descendants have 60 s to end. The supervisor lists any still
  running on stderr, as `ps -o pid,ppid,pgid,lstart,args` rows with the parent and group each
  had when first seen, and stops them: SIGTERM, then SIGKILL 10 s later, or 120 s for a foreground
  run, which restores what it changed on the Mac first
  ([second Mac foreground](foreground.md#waiting-for-the-foreground)). The root's status counts
  once they are gone; a process that survives SIGKILL is still awaited. A lane's `timeout` bounds
  only the root, so a root that exits in time keeps its status through the grace. Agents
  reported Xcode's `DTServiceHub` outliving an ad hoc `xcodebuild … test` on a fresh iOS
  simulator by 10 to 15 minutes, ignoring SIGTERM, until they killed it by hand (EPUBLib, macOS
  27.0, Xcode 27.1, October 2026).
- Xcode's own programs have no grace. When every descendant still running is a program inside an
  Xcode (`…/Xcode*.app/Contents/…`, other than `xcodebuild`) that launchd adopted, the supervisor
  stops them as soon as the root exits, with SIGTERM, then SIGKILL after the same 10 s. Nothing of
  the run can use them by then, since the supervisor returns only once they are gone. Anything else
  still running, beside them or alone, keeps the whole 60 s. Between 2026-10-31 and 2026-11-04 the
  queue's lane logs held 44 stops after the grace, each of a `DTServiceHub` that `xcodebuild … test`
  left or an `ibtoold` that a build left. Each held its lane and heavy slot about 70 s after the
  work had ended: in BreezEPUB's `tests` lane, about 70 s of a 100 s lane
  ([vocaro-engineering#154](https://github.com/vocaro/vocaro-engineering/issues/154)).
- A command that cannot start, such as a mistyped name or a script without its executable bit,
  raises `NotStarted` and releases admission. `Popen` reaps the child whose exec failed before
  it raises, so nothing is left to inspect; keeping the guard would stop every session's
  admission over a typo.
- `qk admit`, `qk run` and the resource CLI carry out SIGTERM, SIGINT and SIGHUP as a stop
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
  ended, for at most `SETTLE_WAIT` (30 s) more. Such an owner is an `qk admit` or resource CLI
  run inside a command that holds no admission, such as a `self` lane's script, so it has a guard
  entry of its own. It has received the signal too, so it stops its own command, with its own
  10 s to SIGKILL and 5 s after, and then removes its entry, or keeps it uncertain if a process
  of its command is left. Then SIGKILL reaches what remains, an owner that never settled
  included, which leaves its entry for recovery; a stopped or timed-out run is
  uncertain if any process is still running 5 s after that SIGKILL. Until 2026-10-27 SIGKILL
  came 10 s after the stop whatever ran, so a lane whose script admits its own work, as
  BreezEPUB's book-opening lane does, could end its `qk admit` while Xcode's tools were still
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
only once the real descendant has published its identity ([app testing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/app-testing.md)).
A bound the supervisor enforces, such as SIGKILL's wait for an admission owner, is checked at
each sample: once a sample comes after the bound, the supervisor must act before the next one.
A wall-clock limit on the whole run failed on the second Mac when the run stalled for a minute
without the load average showing it (#314).

## Lock reclamation

Three kinds of marker coexist, and they are reclaimed differently:

- Admission's guard records each owner's PID, process start, label, whether it is exclusive or
  holds the model, its admission time and an uncertainty flag; beside it, each entry's process
  record lists what supervision saw of that owner's commands. It is
  `/tmp/queuekeeper-gate-suite.lock.warm-push-guard`, named after StudyWright's former host gate
  lock, or `QUEUEKEEPER_GATE_LOCK_FILE` with `.warm-push-guard` appended: StudyWright's gate checks an
  inherited admission against that path, and tests isolate admission by redirecting it. This
  repository's test modules each import `tests/host_isolation.py`, which redirects it and
  `HEAVY_SLOT_DIR` for the whole process, and `test_host_isolation` fails a module that does not
  ([vocaro-engineering#88](https://github.com/vocaro/vocaro-engineering/issues/88)): an entry a
  test leaked had no process record, and it blocked the host's heavy work until removed by hand. Admission takes no lock at the gate lock's own path, and a file
  left there holds nothing back. The guard is never reclaimed by PID or by time, because a dead
  owner's compilers or simulator children can outlive it. A dead or uncertain owner's entry that
  recovery leaves counts as the slots it held, every slot for exclusive work, and the model if it
  held it ([heavy-work admission](#heavy-work-admission)). Admission refuses to proceed while the
  guard is unreadable or not in admission's format, which it never overwrites. Admission's format is JSON with `"vocaroEngineering": 1` and
  `owners`, an object of entries by identity, each with a positive integer `pid`, a `start`
  string, a `label` string and an `exclusive` flag, which every toolkit has written, and any
  `model` or `uncertain` flag or `admitted` string it has; other fields, which a newer toolkit
  may add, are read past. A guard is out of that format as a whole when one entry is, or when
  `owners` is missing or anything but an object, an empty list among them
  ([vocaro-engineering#72](https://github.com/vocaro/vocaro-engineering/issues/72)): admission
  and `qk admissions recover` refuse it, and nested work inside an admission it records fails
  rather than reusing that admission.
- The exclusive queue (`exclusive-queue.json` in the slot directory, changed only under its
  `coordination.lock`) records each waiting request's PID, process start, label, `queuedAt` arrival
  and `seen` last attempt. Ordinary records carry `ordinary`; whole-host records carry `wholeHost`. A
  queued request holds no slot and has started nothing, so admission drops one whose process has
  ended or whose PID has another start time, and a request that times out or is stopped takes
  itself out, unless the overall caller keeps its place across local waits; one whose `seen` is
  more than HEAVY_PLACE_SECONDS old holds nothing back ([heavy-work admission](#heavy-work-admission)).
  An older toolkit reads an ordinary record as an exclusive one and may hold later work back
  more conservatively until the record leaves; upgrade the running toolkit copies together.
- The device record's lock (`device-handoff.json.lock` beside the record) is an `flock` held for
  one change. The kernel releases it when its process ends, so it needs no reclamation, and its
  file is never removed. A reservation in the record ends only by its owner's release or an
  attended recovery ([device reservations](#device-reservations)).

A job leaving admission changes only its own guard entry, whatever state the other owners are
in: a clean exit, or a stop or timeout that saw all of its processes end, removes the entry,
and the guard with its last one; any other exception marks it uncertain. It never changes or
drops another owner's entry. A guard that is unreadable or not in admission's format it leaves
as it is, its own entry in it, for inspection with the rest. Until 2026-10-22 a job that left
while another owner was dead changed nothing, so its own entry died with its process: one lane
killed by its agent stranded a run that finished normally beside it, and the host admitted no
heavy work for 80 minutes, until the guard was recovered by hand.

`qk admit`, `qk run` and the resource CLI carry out SIGTERM, SIGINT and SIGHUP as a stop. A
stop while queued leaves nothing to release. Once admitted, the job first marks its entry
uncertain, in case SIGKILL follows, then stops its command
([process supervision](#process-supervision)). When the command and every observed descendant
have ended, and the final census finds nothing left in their process groups and sessions,
admission removes the entry and the process ends by the signal it received; if any is still
running 5 s after SIGKILL, or the census finds one, the entry stays uncertain for
[recovery](#lock-reclamation). SIGKILL itself cannot be caught: a killed job's entry stays, dead,
for recovery. Until 2026-10-22 none of these signals was handled. SIGTERM and SIGHUP ended the
admitting process at once, leaving its entry dead and its command, in its own session, running
unsupervised; SIGINT stopped only the command's process group and left the entry uncertain.

A job admitted inside another supervised command, as when a `self` lane's script runs `qk admit`,
is stopped in this order. The outer supervisor sends the stop's signal to the lane's processes,
the nested `qk admit` and its command among them, and leaves the nested job's later children
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
Three were inside `qk admit`, running a VoiceOver simulator session and two `swift test` runs, and
their entries kept every session on the host from heavy work for more than two hours, until the
owner approved removing the guard. The same subagents ending at the account's usage limit
(HTTP 429) left nothing behind three times, as a subagent's end stops nothing. To wind subagents
down, ask each to start nothing new, stop only its commands still waiting for admission, which
hold no entry, and let its admitted commands finish. On 2026-10-23 that left the guard empty, and
a gate already running released normally. A person stopping a subagent from the app sends the
same. A subagent stopped while its foreground command ran: that command and its background one,
each with its child in its own session, got SIGTERM within 6 ms and were killed 1.49 s later. A
subagent that had ended its turn and waited on its own background command was still listed, under
the title it gave itself, and stopping it 115 s later did the same to that command (2026-10-31,
Claude desktop app 4.4212.0, Claude Code 2.1.309, macOS 27.2).

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

Quitting the Claude desktop app stops each session's background commands as a task stop does: a
background command and its child in its own session received SIGTERM in the same millisecond and
were killed 1.49 and 1.50 s later, and the shell was gone at +0.006 s. The relaunched app reported
the command as not finished before its session ended (2026-10-31, Claude desktop app 4.4212.0,
Claude Code 2.1.309, macOS 27.2). Ask the owner to quit the app only once no admitted command is
running.

Pressing the Stop button stops only the turn's foreground command, as an interrupt does. The
command and its child in its own session received SIGTERM in the same millisecond and were gone
1.47 and 1.48 s later without exiting, killed; the shell was gone within 5 ms. The session's
background command received nothing and ran on for 147 s, until a later action on that session,
not identified, stopped it the same way (2026-10-29, Claude desktop app 4.29.0 running Claude Code
2.1.309, macOS 27.2).

Claude Code 2.1.308's code for stopping a shell command explains the second interrupt. It signals
the shell's process group and each descendant that one `ps -A` scan lists, but abandons a scan
that takes longer than 500 ms and then signals the group alone. SIGKILL follows 1.5 s after the
stop begins, to the group, to the descendants found and to a fresh scan from the shell, which has
usually exited by then. The host's load average was above 100 during these probes. `qk admit`
runs in the shell's process group, so it always receives the SIGTERM and stops its command, but
it has 1.0 to 1.5 s before its own SIGKILL, and nothing escalates after that. A command that has
not ended by then leaves its entry uncertain and, if the scan missed it, runs on unsupervised.

Codex does not time a command out. Its shell tool returns once the requested wait passes and
leaves the command running, unsignaled, under a session handle. When the Codex session ends, the
command, which leads its own session, is killed with SIGKILL and no SIGTERM first; its child in
its own session receives nothing and runs on. `codex exec` ends its session with its turn: in two
runs the command was killed without a logged signal, in the watched one 5 ms after the turn
completed (2026-10-23, Codex 0.163.0-alpha.11.2 as ChatGPT's Codex Desktop bundles it, run as
`codex exec`, macOS 27.0). An `qk admit` killed this way never marks its entry uncertain: the
entry stays, dead, and its command runs on unsupervised. A Codex Desktop thread keeps the
command past its turn: one a new local thread started and yielded on ran its full 900 s after the
turn ended, with its child in its own session, and neither received a signal; the app listed it
under the thread's background processes (2026-10-29, ChatGPT 26.1023.51421 with codex-cli
0.165.0-alpha.1.2, macOS 27.2). Archiving the thread ends its session: the command's shell and
the command were killed with SIGKILL and no SIGTERM within 2.3 s of the click on the row's archive
button, and the child in its own session ran on, reparented to launchd. Closing the app's window
stops nothing: 20 s after the window closed, the shell, still a child of Codex's app server, and
both probe processes ran on unsignaled (2026-10-31, ChatGPT 26.1027.30824 with codex-cli
0.166.0-alpha.5.1, macOS 27.2). A Codex agent therefore follows an admitted command to its
terminal result before ending its turn under `codex exec`, and before the thread is archived in
Codex Desktop.

Admission recovers a dead or uncertain guard entry itself, by the checks below, before it
counts the entry against a request: it prints `heavy: Recovered LABEL (PID …): …`, logs a `recovered` line
marked `automatic` ([admission log](#admission-log)) and goes on. An entry those checks leave is
checked again after HEAVY_RECOVER_SECONDS (60), since a census reads every process on the host;
meanwhile it counts as the slots it held, and a waiting message that counts it says why it
stayed ([heavy-work admission](#heavy-work-admission)). Until 2026-11-01 recovery was attended only, and a
task stop's SIGKILL left entries whose commands had all ended refusing heavy work on both Macs
until the owner removed them; agents could not always run the command themselves.

`qk admissions recover` carries out the same recovery by hand. The first command below lists each entry that is dead or uncertain, with its identity, label,
PID and how long ago it was admitted (for an entry an older toolkit wrote, how long ago its owner
started), whether its owner still runs, and whether it has a process record. The second recovers
one entry, named by its owner's PID or by its identity:

```sh
python3 scripts/qk admissions recover
python3 scripts/qk admissions recover 56766
```

Admission publishes a complete opening process record before it publishes the guard. If it cannot
write the opening record, the request gives up its slots before any work starts (#336). An empty or
missing record from an earlier or interrupted run cannot prove that no command ran.

It removes that entry alone, and the guard with its last entry, deletes the entry's process record
and logs a `recovered` line ([admission log](#admission-log)), only when all of these hold:

- Its owner has ended: its PID is gone, or `ps` reads another start there. A live owner's entry is
  never touched, uncertain or not, named by PID or by identity: the owner removes it, or leaves it,
  when it ends. A `ps` that cannot say leaves the owner taken as running.
- It has a process record that reads whole and names the process of each command it has not
  settled, or that names no command of an owner that supervises every command it starts.
- The census a run ends with ([process supervision](#process-supervision)), run against that
  record, finds nothing. It checks recorded processes by PID and start, and live processes in
  groups or sessions an unsettled command used. It skips an ID that is now a live process's PID
  and skips a process for a group or session if its birth follows the recorded close for every
  unsettled command that used that ID. Without a close time for any such command, the space is
  still checked without a birth cutoff.
- For a record that names no command, the owner ended before its first: `qk admit`, `qk run`
  and the resource CLI start every command through `processes.supervise`, which records it before
  starting it in a session of its own, so whatever the owner started before then is in the
  owner's process group, or in its session where the group leads it. The census finds nothing
  there that started no earlier than the owner, the owner's recorded ancestors and the group's or
  session's leader aside. It errs toward keeping the entry: a sibling the same shell started in
  that group after the owner, such as another lane started beside it, keeps it until it ends. A
  helper that left both group and session before the first command is not seen; none of the
  toolkit's does. On 2026-11-07 three lanes killed between admission and their first recorded
  command left entries with no record, and BreezEPUB's gate waited 86 minutes behind them until a
  session removed them by hand
  ([vocaro-engineering#315](https://github.com/vocaro/vocaro-engineering/issues/315)).

Otherwise it changes nothing, exits 1 and says why: each process still running, by PID with why it
counts and a `ps` listing, or what to check by hand. Stop or wait for what still runs, then run it
again. It leaves alone, too, a guard that is unreadable or not in admission's format.

An entry recovery leaves because nothing proves it finished, one with no process record or one
whose record cannot vouch for every command, is removed by hand, once you have checked that
nothing its owner started still runs:

```sh
python3 scripts/qk admissions remove 56766 --why "ps shows nothing of BreezEPUB ways-in"
```

It runs recovery first: an entry recovery can prove finished is recovered as above, and one whose
owner, or a process its census names, still runs stays. Then it checks what of the process record
can be read: each recorded process by PID and start, each live process in a group or session a
recorded command used, and anything started since the owner in the owner's process group, or in
its session where the group leads it, as the admission line records them, or, without one, in a
group or session the owner led. While any of these runs, it names each by PID with a `ps` listing,
removes nothing and exits 1. An owner without a record that led no group or session, as one a
shell started in the shell's group, leaves nothing to check there, which is why the check by hand
comes first. Otherwise it removes the entry, and the guard with its last one, deletes its record,
and logs a `recovered` line marked `byHand`, with why recovery left the entry and the `--why`
given ([admission log](#admission-log)). It is deliberately a command of its own, outside this
repository's allow rule for `qk admissions recover`, whose basis is that recovery removes only
provably finished entries: a session's auto mode asks before it removes one that is not (owner's
decision, 2026-11-08,
[vocaro-engineering#315](https://github.com/vocaro/vocaro-engineering/issues/315)). Until then
such an entry was removed by editing the guard.

The process record is `<guard>.IDENTITY.jsonl`, beside the guard, named by the identity that keys
the entry. Admission writes its first line under the coordination lock just before it adds the
entry, so that no entry is published without a record, even one whose owner is killed before its
first command: `admission`, the identity; `owner`, as `[PID, start]`; the owner's process `group`
and `session`; the `ancestors` it was started under, as `[PID, start]`; and `supervised`, whether
the owner starts every command through `processes.supervise`, as `qk admit`, `qk run` and the
resource CLI do (`Admission(supervised=True)`). A toolkit older than 2026-11-08 cannot read that
line, and so leaves such an entry for a check by hand. The toolkit's supervision in the admitting process (`qk admit`, `qk run`, the
resource CLI, and `processes.supervise` inside an `Admission` the same process holds) appends JSON
lines to it, each naming one command by a key of its own: `argv` before the command starts; `root`,
its PID, once started; at each sample that sees something new, the `processes` first seen, as
`[PID, start]`, and the process groups and sessions first seen, as `spaces`; `closed` updates as
`[ID, epoch seconds]` when first seen without the command, or `[ID, null]` when used again; and
`settled` once the run confirms that the command ended, by exiting, or by a stop or timeout that
saw it and every observed descendant end and whose census found nothing. Recovery checks no
settled command, so a daemon a passing command starts on purpose does not keep the entry or the run
uncertain. Nested work in another process reuses the admission and writes nothing to the record,
since the outer supervisor sees its processes. Release deletes the record after the entry; an
uncertain end keeps it. A line that cannot be written deletes the record and ends recording, with
`heavy: the process record was not written`, so recovery never trusts a record that lacks a line.

A caller that supervises its own commands inside an `Admission` its process holds, as
StudyWright's push keeper does, appends the same lines itself, each a dictionary passed to
`Admission.observe`: `command` and `argv` before the command starts, so that a record without its
`root` says it may have started; `root`, with `spaces` holding it, as soon as it starts; each
process its samples see, with the start `ps -o lstart=` reads, and each group and session; and
`settled` only once a passing exit, or a failure whose census found nothing, confirms the end.
Recovery then checks its entry as it checks supervision's.

An entry without a process record is left for a check by hand: one an older toolkit wrote, one whose
record could not be written, and one whose caller runs its own commands without recording them, as
an older StudyWright push keeper does; a record that holds only its admission line, unsupervised,
counts as none, since such a caller's commands may be running unrecorded.
Inspect its recorded owner, that owner's command processes and any reserved tree; stop or wait for
all of the transaction's children; then remove the entry by hand. A push keeper's `owner.json`, in
its checkout's transaction lock, lists each command the keeper ran with its PID, process group,
start time and observed descendants. A free owned descriptor or a later foreign acquisition, checked
against the recorded process birth and descriptor nonce, establishes that an old lock was released.
Preserve every foreign reservation. Time passing alone establishes none of this. Recovering the
guard those subagent stops left (2026-10-22) found all three recorded PIDs dead, no swift-frontend,
xctest, xcodebuild, DTServiceHub or simctl process left, and no slot file held open. The VoiceOver
run had left two booted simulators, which its own logs proved its, so they were deleted by UDID. On
2026-10-27 a persistent push keeper that an agent's task stop ended ten minutes into its gate had
left nothing running: a census against its `owner.json` checked 22,992 PIDs with their start times
and 8,088 process groups and found no survivor, and the host still admitted no heavy work until the
entry was removed by hand.

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
K=~/Development/queuekeeper/src/python/StopSignals D=/private/tmp/stop-probe/NAME
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

Computer use drives Codex Desktop (ChatGPT, `com.openai.codex`) for these probes, under a
`mac-foreground` reservation. Its background tools reach the composer: type into the focused field
and press Return, since the Send button ignores the accessibility press. A thread row's archive
button and File > Close ignore background presses too, so archiving a thread or closing its window
takes one full-screen click each (2026-10-31, ChatGPT 26.1027.30824). Computer use grants the
Claude desktop app only read access, and quitting it ends the driving session, so a stop or quit
in that app is the owner's to perform: the session starts the watcher and the probe, the owner
acts, and the session reads the report afterwards. A thread a probe started is left in the app;
archive it once its probe has ended.

## Ending a coordinated command

Use the supplied `qk run` or resource CLI for commands. They return the child's status after
leaving admission normally. If using Admission directly, leave the context before raising SystemExit
for a nonzero result. An exception inside the context retains its uncertain guard, by design,
because the coordinator cannot infer whether that exception left children running. Admission used
directly handles no signals. To stop as the supplied commands do, enter
`processes.forwarding_signals()` around it and pass `on_stop=admission.interrupted` to `supervise`.
`supervise` called in the process that holds the admission writes its process record. A caller that
supervises its own commands writes the same lines with `Admission.observe` ([lock
reclamation](#lock-reclamation)); when one writes none, an entry its killed process leaves is
recovered by hand. The record holds only what was written to it, so a caller that also starts
long-lived processes another way inside the admission leaves recovery blind to them: run them
through `supervise`, or record them, too.

Legacy shell gates authenticate inherited exclusive admission for their exact lock namespace.
The coordinator does not export a lock opt-out into the suite: regression fixtures retain
independent real locks. A process wrapper must forward the admission descriptors, as the
shared process supervisor does; copying the token environment alone does not transfer ownership.
