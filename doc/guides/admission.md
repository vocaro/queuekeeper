# Heavy-work admission

This guide is the detail behind [lanes and admission](../user_guide.md#admission): how each kind of
request is admitted, the limits a Mac admits under, the order of its queue, what a waiting message
means, what the admission log records and how to see who holds a Mac. Read the user guide first;
come here when a wait needs explaining, a lane needs a resource chosen, or a Mac's limits need
changing. [Scheduling](scheduling.md#policies-in-force) states the policy and why each rule was
made; this guide states the mechanism.

## Run heavy work through admission

On the shared host, builds, `swift test`, `xcodebuild test`, gate suites, corpus lanes and
conversions all take admission: one host-wide queue with bounded concurrency. A private slot pool,
such as `mkdir` locks in a scratch directory, is a second queue that ignores load and memory
pressure and that exclusive work cannot exclude; one ran the load average to 764 (October 2026).
Project adapters (a `heavy_slot.py` shim, scripts that import `queuekeeper.resources`) all call
`src/python/queuekeeper/resources.py`. Public project documentation may still show plain commands
for outside contributors: the rule is about this host.

- **Lanes.** `python3 scripts/qk run LANE` runs a configured lane. A `resource` of `heavy` or unset
  takes one slot, `exclusive` the host, and `self` runs directly, since its script admits each of
  its parts. `light` loads as `heavy`, so a project may drop it.
- **First come, first served.** A short check, such as docs or spelling, waits for a slot like any
  other lane, preflight lanes and `qk run spelling` among them (owner's decisions, 2026-11-07,
  [vocaro-engineering#200](https://github.com/vocaro/vocaro-engineering/issues/200), and 2026-11-09,
  [vocaro-engineering#305](https://github.com/vocaro/vocaro-engineering/issues/305)).
- **Arguments.** Arguments after `--`, as in `qk run test -- --filter SUITE`, are appended to the
  lane's command. `--expect N` requires exactly N executed tests; a run that selected N but left
  some skipped or canceled exits 3, unmeasured, and any other count fails
  ([verifying](../user_guide.md#verifying), vocaro/queuekeeper#181).
- **Environment.** A session's environment does not travel to another Mac, so a test that needs a
  variable, such as a corpus path, gets it from its lane's command
  ([vocaro-engineering#285](https://github.com/vocaro/vocaro-engineering/issues/285)).
- **Other commands.** Run any other heavy command with `qk admit`. `--timeout` bounds the wait.
  The command runs in the current directory with its output on the terminal; like a heavy lane,
  `qk admit` first refreshes the operating policy and records a receipt in
  `.build/queuekeeper/admit/source.json`. It needs no `PYTHONPATH` and works from a session
  worktree ([locating the toolkit](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/adoption.md#locating-the-toolkit-from-a-worktree)).

```sh
python3 scripts/qk admit -- \
  xcodebuild test -scheme SCHEME -destination "platform=iOS Simulator,id=UDID"
```

A toolkit clone older than `admit` reaches the same queue through the resource CLI:

```sh
PYTHONPATH="$QUEUEKEEPER_ROOT/src/python" python3 -m queuekeeper.resources -- COMMAND
```

**Queuekeeper's own runners.** `qk unittest`, `qk xctest`, `qk simulator-tests` and
`qk unity test` run inside the admission of the lane or `qk admit` that started them. Started by
themselves, they are admitted and placed as `qk admit -- COMMAND` would be; in a folder with no
`queuekeeper.json` they wait for a slot on this Mac. `--unadmitted`, written after the runner's
name, runs one with no admission, which only a test of the runner itself needs:

```sh
python3 scripts/qk unittest --unadmitted -s tests test_core.ConfigurationTests
```

A command that is not Queuekeeper's, such as a bare `python3 -m unittest` or `swift test`, takes
no slot unless run with `qk admit`. The toolkit's own tests, run that way, say so once on standard
error from `tests/host_isolation.py` ([verifying](../user_guide.md#verifying), vocaro/queuekeeper#36).

## Choose the request

| Request | How | For |
| --- | --- | --- |
| Heavy | `qk admit`, or `"resource": "heavy"` | Builds, tests, setup, recovery, functional runs |
| Model | `--model`, or `"model": true` beside `"resource": "heavy"` | On-device generation, MLX, Core ML on the GPU ([model admission](#model-admission)) |
| Whole-host | `"wholeHost": true` on the lane or its group | A gate that runs the project's suites side by side |
| Exclusive | `--exclusive`, or `"resource": "exclusive"` | A timed or whole-machine measurement only |

**Only the timed part is exclusive.** Build, install and set up under `qk admit --here`, beside
other work, then admit the timed run alone with `qk admit --here --exclusive`, such as
`xcodebuild build-for-testing` and then `test-without-building`. Both take `--here` because each
`qk admit` chooses its Mac on its own; a `self` lane's parts stay together without it
([long-running measurements](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/shared-host.md#long-running-measurements)).

- An exclusive request inside heavy admission is refused, so a lane that needs both is `self` and
  its script admits each part, as BreezEPUB's `book-opening` and PDFReflowLib's
  `memory-platform-calibration` do. A lane group can be neither `self` nor run a `self` lane, since
  its one admission would hold the script's own.
- `qk doctor` warns of a `self` lane whose command neither calls `qk admit` nor runs a checkout file
  that does, since Queuekeeper trusts the script. The check reads text: it catches a lane that
  admits nothing, not one that admits only some parts.
- **A gate suite is never exclusive.** `qk admit --exclusive` and `qk remote admit --exclusive`
  refuse a command that is word for word a heavy lane's `command` or `compileFirst`, and name the
  lane: run it with `qk run LANE`, or give the lane `"resource": "exclusive"` if it truly needs the
  host. An exclusive gate held every slot of another Mac for 33 minutes (2026-11-12).
- **A check that voids itself on a busy Mac does not belong in a gate suite**, because the suite
  shares the Mac and admission judges only a run's start. Either it asks for the host with
  `qk admit --here --exclusive` from a `self` lane, or it moves to its own exclusive lane that
  `queue.lanes` names after the gate. mpm's self-voiding check held its queue an afternoon
  (vocaro/mpm#108, vocaro/queuekeeper#28).
- **A gate tolerates a busy host.** Its time limits leave out
  [core-wait time](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/measurement.md#cpu-scheduling-and-core-wait-time),
  a ceiling that memory pressure spoiled is
  [unmeasured](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/measurement.md#host-pressure-and-unmeasured-ceilings),
  not failed, and its lane exits 3
  ([honest outcome states](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/evidence.md#honest-outcome-states)).
  A gate whose tests still assume a quiet host stays exclusive until they are fixed.

### Whole-host and exclusive

A **whole-host** lane starts once every guaranteed slot is free, beside lighter work already
running beyond them, and from then on no other heavy work is admitted on that Mac, not even beyond
the guaranteed slots. Two never run on one Mac at once. While it waits, it keeps the guaranteed
slots for itself ([queue order](#queue-order)). It is for a gate, which needs the Mac to itself but
must not stall every other session while it waits. A gate's load dips between phases, and work
admitted on a dip left a gate unmeasured (owner's decision, 2026-11-05,
[vocaro-engineering#196](https://github.com/vocaro/vocaro-engineering/issues/196)).

**Exclusive** work holds every slot up to the maximum and the model, is admitted one request at a
time in arrival order once the work before it has ended, holds every later request back, and
[settles](#exclusive-work-settles-first) before its command starts. A whole-host lane cannot also
be exclusive. In the choice of Mac the two count alike: a Mac running either has no room for any
heavy lane ([which Mac runs a lane](another-mac.md#which-mac-runs-a-lane)).

### Bound a job's own parallelism

Admission counts a gate as one job however many workers it starts, so bound its build and test
parallelism ([parallel tests](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/parallel-tests.md#bound-what-runs-at-once)).
`--jobs` does not bound everything (measured with Swift 6.4 and Xcode 27.1, 2026-10-23):

- A debug `swift test --jobs 2` is bounded.
- A release `swift test -c release --jobs 2` is not and cannot be today: Swift Build sets
  `-num-threads` to the core count after any value passed with `-Xswiftc`. A project whose gate
  builds in release documents its compiles as unbounded rather than passing a flag that does
  nothing.
- `xcodebuild test` takes a build task per core unless given `-jobs N`.
- A test that fans out with `DispatchQueue.concurrentPerform` bounds its own fan-out.

A build's `-num-threads` values are in its Swift Build task store:

```sh
strings -n 1 .build/SCRATCH/out/Intermediates.noindex/XCBuildData/*.xcbuilddata/task-store.msgpack \
  | grep -A1 -- '^-num-threads$'
```

## Admission limits

Ten unbounded builds panicked a Mac (2026-10-14), and a fixed limit of three then left it idle, so
a Mac admits by guaranteed slots plus adaptive ones:

| Limit | Default | Meaning |
| --- | --- | --- |
| HEAVY_SLOTS | 3 | Guaranteed slots |
| HEAVY_SLOTS_MAX | half the CPU count | Most slots in all; set it to HEAVY_SLOTS for a fixed limit |
| HEAVY_BUSY_LIMIT | 90 | The CPU's busy share, in percent, at or over which the Mac admits only its first job |
| HEAVY_SETTLE_SECONDS | 30 | How long after a slot is taken before another beyond the guaranteed ones |
| HEAVY_QUIET_LIMIT | 25 | Busy share an exclusive admission waits to fall below |
| HEAVY_QUIET_SECONDS | 300 | Longest an exclusive admission waits for that |
| HEAVY_PLACE_SECONDS | 180 | How long a queued place holds others back without renewal |
| HEAVY_RECOVER_SECONDS | 60 | How often admission rechecks a stranded entry |

- **Guaranteed slots** admit while the busy share is under HEAVY_BUSY_LIMIT, and the first job
  whatever it reads.
- **At or over the CPU limit** a Mac admits a job only while nothing admitted runs there:
  `the CPU is 92% busy, at or above HEAVY_BUSY_LIMIT=70% (this Mac's limit, set in PATH), and
  admitted work runs here: over its CPU limit a Mac admits only its first job`. The first job is
  kept so that load which never asks, such as the owner's own work, can delay admitted work but
  never stop all of it. A whole-host lane is bound the same way. Three wide lanes had held a Mac
  over its limit (owner's decision, 2026-11-17,
  [vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211)).
- **Beyond the guaranteed slots**, one job at a time is admitted up to HEAVY_SLOTS_MAX while the
  busy share is under the limit, memory pressure is normal (`kern.memorystatus_vm_pressure_level`
  is 1), and no slot was taken in the last HEAVY_SETTLE_SECONDS.
- **The busy share** is the CPU's time outside idle over about the last 15 seconds, from the
  kernel's tick counters, shared through `cpu-samples.json` in the slot directory. A request's
  first five seconds of samples withhold a slot as `settle`. While the share cannot be read, a
  guaranteed slot opens anyway. It replaced the one-minute load average, which counts simulators'
  daemons and read 27 to 66 on a half-idle Mac; the load is still logged for audits
  ([vocaro-engineering#200](https://github.com/vocaro/vocaro-engineering/issues/200)).
- **Work runs on a Mac** while its guard records an admission or a slot is held; an entry recovery
  left whose owner has ended is neither.
- **No preemption.** A job admitted while the host was quiet may become heavy later.

The full policy and its reasons are in [scheduling](scheduling.md#on-one-mac).

### Exclusive work settles first

An exclusive admission comes the moment the work before it ends, while trailing measures still
count that work: mpm's timed gates voided that way (vocaro/queuekeeper#121). So, holding every
slot, it waits while the busy share is at or above HEAVY_QUIET_LIMIT, for at most
HEAVY_QUIET_SECONDS, then starts however busy the Mac reads. It says so on standard error
(`heavy: waiting for the Mac to settle: …`, `heavy: the Mac settled after 14s: …` or
`heavy: starting without a quiet Mac: …`), and its `ended` line in the
[admission log](#admission-log) records the wait. A stop while it settles releases every slot with
nothing run. HEAVY_QUIET_SECONDS=0 starts at once. The two quiet limits are environment variables
only, not keys of the limits file.

The command is then told what admission read, so that a check judges that reading rather than
taking its own:

- `QUEUEKEEPER_HOST_BUSY`: the busy share in percent; `QUEUEKEEPER_HOST_BUSY_SECONDS`: the seconds
  it spans.
- `QUEUEKEEPER_HOST_LOAD`: the one-minute load.
- `QUEUEKEEPER_HOST_SETTLED_SECONDS`: how long it waited.
- `QUEUEKEEPER_HOST_QUIET`: 1 when the Mac read quiet, 0 when it started at the bound or could not
  read the busy share.

A value that could not be read is unset. A check that voids a timed run reads
`QUEUEKEEPER_HOST_QUIET` and needs no wait of its own; one that judges the load average still sees
the work admission waited out (vocaro/mpm#108). Only exclusive work settles and is told.

### The limits file

A Mac's own limits go in `admission-limits.json` beside the admission log, in
`~/Library/Application Support/queuekeeper/` (an isolated HEAVY_SLOT_DIR keeps its own), which
every process on that Mac reads.

- **Keys.** `slots`, `slotsMax`, `busyLimit` and `settleSeconds`. Each is a number, or
  `{"perCore": F}` for F times the CPU count; a slot count is rounded down and at least 1. A key
  left out keeps its default. For example, `{"slots": 4, "busyLimit": 85}`. `busyLimit` is a
  percentage; the former `loadLimit` is an unknown key.
- **A file admission cannot read**, with an unknown key or a value of the wrong kind, refuses every
  request on that Mac with an error naming the file, so its owner fixes it rather than Queuekeeper
  guessing.
- **A new key is a breaking change** for every older copy of the toolkit, which would refuse all
  heavy work on that Mac. Write one only once nothing there runs an older copy: work started before
  the key landed has ended, no `QUEUEKEEPER_ROOT` names an older copy, and no session worktree of
  the toolkit is behind the landing. `busyLimit` (2026-11-06, vocaro-engineering#200) was the first.
- **To load a Mac more**, raise `slotsMax` and `busyLimit`, not `slots`: a whole-host lane needs
  every guaranteed slot free, so each one added sends gates elsewhere (2026-11-07). Another Mac is
  to be loaded as much as possible and this Mac a little less.
- **See the values** with `qk admissions usage` for every Mac and `qk admissions limits` for one
  Mac's with where each comes from. This guide does not copy them.
- **Environment overrides.** A limit's environment variable overrides the file for its process.
  Set one for every session or none: an exclusive job holds only the slots below its own maximum,
  and a caller with a higher one can take slots above it. Waiting messages name a limit by its
  variable whatever set it.

### Setup's CPU limit

`python3 tools/install-checkout.py` takes the Mac it runs on to be the owner's workstation and
writes `"busyLimit": 70` into its limits file. Nothing else in admission or placement names the
workstation; its lower limit is only its setting (owner's decision, 2026-11-17,
[vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211)). Setup never changes a
file that already sets `busyLimit`, adds no other key, and leaves an unreadable file as it is,
reporting it and exiting 1. `--check` prints the limit in force and its source; `--remove` leaves
the file. Three cases take a hand:

- A Mac that ran Queuekeeper before 2026-11-17 with no `busyLimit` had 75 and now has the default
  of 90. Run setup there again for 70, or write the value wanted.
- Setup run on a Mac that only takes work writes 70 there too. Write that Mac's `busyLimit` by hand
  when it should differ.
- `qk farm add` writes nothing on the Mac it adds: its limit is the default until its file says
  otherwise.

## Queue order

Every request records its arrival in `exclusive-queue.json` in the slot directory. A queued request
holds no slots. Admission drops one whose process has ended or whose PID has a different start
time; a stop removes its record. Each request still needs its own resources: order does not create
a free checkout, unlock a screen or release a model lock.

- **Arrival order holds across kinds.** An older one-slot request runs before a later whole-host or
  exclusive one, even when it cannot start yet; one-slot requests share slots with each other
  (owner's decision, 2026-11-17, [vocaro/queuekeeper#171](https://github.com/vocaro/queuekeeper/issues/171);
  [D2](scheduling.md#d2-may-a-gate-pass-one-slot-work-queued-ahead-of-it)).
- **No class by who asks.** A session's hand-run lane may hold a guaranteed slot while a queue's
  gate waits (same decision; [D3](scheduling.md#d3-may-hand-run-lanes-hold-guaranteed-slots-while-a-gate-waits)).
- **An older exclusive request** holds every later request back while the slots it needs drain.
  Jobs already running finish normally.
- **An older whole-host request** holds back less. Later exclusive and whole-host requests wait
  their turn, but other heavy work may still take a slot beyond the guaranteed ones while the Mac
  has spare capacity, and takes none of the guaranteed slots, so they drain toward the gate
  (owner's decision, 2026-11-05,
  [vocaro-engineering#174](https://github.com/vocaro/vocaro-engineering/issues/174)). Where
  HEAVY_SLOTS_MAX equals HEAVY_SLOTS, or there is no spare capacity, heavy work waits for it as for
  exclusive work. Before this, sessions took each guaranteed slot as it freed and a gate could wait
  out its 5,400 s.
- **A place must be renewed.** A request holds anything back only while its process keeps asking or
  says its work still waits; after HEAVY_PLACE_SECONDS without either it holds nothing back, though
  it keeps its arrival for its next attempt. This applies to exclusive and whole-host requests.

### Keeping a place across offers

`qk run` and `qk admit` keep one place across local waits between offers to other Macs, for every
kind of request, so a lane does not rejoin behind requests that came meanwhile
([which Mac runs a lane](another-mac.md#which-mac-runs-a-lane)).

- A dispatch carries the request's age, translated to the receiving Mac's clock and unchanged by
  retries. A declined offer releases its checkout and record; a later offer restores the age.
- Capacity preflight ignores exclusive requests that arrived later, so they do not keep overtaking
  it. Age sets order, not a deadline.
- Age stops at the receiving CLI step: nested commands keep inherited admission, but independently
  admitted child work, such as each part of a `self` lane, starts with a fresh age.
- **During an offer** `qk run` says every third of HEAVY_PLACE_SECONDS that the lane still waits,
  for as long as its wait record shows another Mac has not started it (`resources.Place.kept`,
  `lane_wait.taken_there`). The place then keeps the guaranteed slots however long the offer takes,
  and stops within 180 s once another Mac's admission is relayed. A gate away on an offer had lost
  its turn twice in 45 minutes ([vocaro/queuekeeper#160](https://github.com/vocaro/queuekeeper/issues/160)).
  A `qk admit` command has no wait record, so its place holds only for 180 s after it last asked.

### A whole-host lane's places on other Macs

A whole-host lane that waits takes a place in the queue of every Mac that can run it, and runs on
the first to admit it (owner's decision, 2026-11-17,
[vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211)). Gates had queued on
one Mac while nothing drained toward them on another. Once its first wait here ends unadmitted,
`qk run` starts a holder on each other Mac of the farm that may run the lane and says
`queuekeeper: gate holds a place in studio's queue while it waits, …`.

- **What a place is.** A queued whole-host request in that Mac's `exclusive-queue.json`, with the
  lane's age and the label `LABEL (waiting, sent by NAME)`. Its owner is the holder, a small process
  the sending Mac starts over SSH; it claims no checkout, runs no `provision` lane and holds no
  slot. Admission there treats it as a gate that waits there.
- **How it is kept.** The sender writes a word to each holder every third of HEAVY_PLACE_SECONDS.
  When the sender ends, its connections close and each holder removes its place at once. A sender
  silent with the connection open holds nothing back after HEAVY_PLACE_SECONDS, and its holder ends
  after twice that. A killed holder leaves a record whose process has ended, which admission drops.
  An unreachable Mac holds no place, said once, and is retried at each renewal.
- **How the lane starts.** While it holds places, the sender reads the Macs every 30 s at most
  (`PLACED_WAIT`). A Mac does not count the lane's own place as a request ahead of it; once it has
  room the lane is offered there, and the run carries the place's name
  (`QUEUEKEEPER_ADMISSION_PLACE`) so its `provision` lane and the lane are admitted past it. What
  the lane's command starts does not inherit that name.
- **It never runs twice.** One process starts the lane, in one place at a time: it waits here or
  offers the lane to another Mac, never both. A place is only a record. Admitted here, the lane
  gives up its other places before its command starts; started elsewhere, within one renewal.
- **What it costs.** While a gate waits, every Mac keeps its guaranteed slots from later one-slot
  work. Read each Mac's whole-host share in `qk admissions usage`, and one-slot work's wait.
- **A lane that needs an unlocked screen** has a place on a Mac only while that Mac's screen reads
  unlocked, by the keys [screen state](screen-state.md#screen-state) reads; `unknown` counts as
  locked. The holder checks as it starts, at each word and every 5 s, and each change is said once
  ([vocaro/queuekeeper#214](https://github.com/vocaro/queuekeeper/issues/214)). This Mac treats its
  own queue the same way: while its screen is locked the lane asks no admission here.
- **What takes no place.** Exclusive work, which joins another Mac's queue only after 900 s of
  declined offers; one-slot work; work asked once with `--if-free`; and a lane that stays here.

## Nested admission

Nested work reuses its inherited admission, whole-host or not, and never obtains a second slot.
Admission exports `QUEUEKEEPER_RESOURCE_TOKEN` and keeps its lock descriptors open across exec. A
nested `qk run`, resource CLI call or project gate validates them against their lock files, the
owner's nonce, its guard entry and its process start time, then runs inside the outer admission
without taking a slot or waiting behind a queued exclusive request. An exclusive request inside
shared admission is refused, as is a model request inside admission that does not hold the model.

The toolkit's process supervisor, and `qk unittest` for each module's process, pass a command only
the token's descriptors that their own process holds, each open on the lock file the token names.
A process started without them, as a plain `subprocess` call starts its child, keeps the token but
not the admission: a nested admission there is refused rather than given a slot.

## The slot directory

The default pool lives in `getconf DARWIN_USER_TEMP_DIR`, under the legacy `pdfreflow-heavy-slots`
name, whatever the command's TMPDIR, so a private TMPDIR joins the same queue. HEAVY_SLOT_DIR
overrides it for isolated tests only; a separate directory must not multiply the host's budget.
An answer from `getconf` that is not an absolute path is refused and nothing is written, since a
relative one would start a second budget in the current folder. A test that mocks `subprocess.run`
and admits work or reads capacity fails, and sets its own `HEAVY_SLOT_DIR`
(vocaro/queuekeeper#161).

## Waiting messages

A wait is expected. Admission prints `heavy: waiting (Ns): REASON` when it starts waiting and every
five minutes after. The reason names the limit that refused the request, its value and what the
attempt read. A HEAVY_SLOTS_MAX or HEAVY_BUSY_LIMIT it quotes is the Mac's, with its source in
parentheses: `(this Mac's limit, set in PATH)`, `(this Mac's limit, set in the environment)` or
`(this Mac's limit, the default, since PATH does not set it)`, PATH being the limits file. A
session had read the Mac's limit as its project's (vocaro/queuekeeper#29). The table shortens the
parentheses to `(…)`:

| Kind | Reason, as printed |
| --- | --- |
| `slots` | `all 3 guaranteed slots are busy and so is every slot up to HEAVY_SLOTS_MAX=9 (…)`, or `… and HEAVY_SLOTS_MAX=3 (…) allows none beyond them`; for a whole-host lane, `a whole-host lane needs every guaranteed slot (HEAVY_SLOTS=3) free, and 1 is busy`; for exclusive work, `exclusive work needs every slot up to HEAVY_SLOTS_MAX=9 (…) free, and 2 are busy` |
| `settle` | `all 3 guaranteed slots are busy and the CPU does not yet show the job admitted 12s ago, within HEAVY_SETTLE_SECONDS=30`, or `… and the CPU has been read over 2s, short of the 5s a reading spans` |
| `cpu` | `the CPU is 92% busy, at or above HEAVY_BUSY_LIMIT=90% (…), and admitted work runs here: over its CPU limit a Mac admits only its first job`, for a request that found a guaranteed slot free; `all 3 guaranteed slots are busy and the CPU is 91% busy, at or above HEAVY_BUSY_LIMIT=90% (…)`, or `… the CPU's busy share, which HEAVY_BUSY_LIMIT=90% (…) bounds, is unavailable`; a toolkit older than vocaro-engineering#200 logged `load` |
| `memory` | `all 3 guaranteed slots are busy and memory pressure is level 2 (warning), not 1 (normal)`, or `… memory pressure, which must be level 1 (normal), cannot be read` |
| `model` | `the on-device model, one run at a time, is held by LABEL` |
| `exclusive` | `it waits behind exclusive work queued ahead of it: LABEL`, or `… holding the host: LABEL` |
| `wholeHost` | `it waits behind a whole-host lane queued ahead of it: LABEL`, for exclusive and whole-host requests; `it waits behind a whole-host lane holding the host: LABEL` for other heavy work while one runs; for other heavy work behind a queued one, `a guaranteed slot is free but kept for a whole-host lane queued ahead of it: LABEL; and …` with why no slot beyond them is free. With every guaranteed slot busy anyway, the reason is another kind's |
| `stranded` | another reason, then `; the guard entry of LABEL, PID 4242, is uncertain and requires inspection (its owner still runs), and is counted as slot 1; …`, with why [recovery](process-supervision.md#lock-reclamation) left it and what it is counted as ([vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)); a model request's reason is `the on-device model, one run at a time, is counted as held` |
| `guard` | `the guard PATH is unreadable or not in admission's format and requires inspection; …` |

In the last two rows `…` names `python3 scripts/qk admissions recover`. Each kind is the one the
[admission log](#admission-log) records.

**When a wait times out** (5,400 s by default), admission fails without reclaiming any lock, naming
the reason, the Mac, the wait and that nothing ran; an exclusive or whole-host request leaves the
queue unless it keeps its place across waits.

- Behind running work it says `Resource queue busy on MAC: REASON; held by LABEL (slot0, slot1);
  waited Ns; nothing ran`, an ordinary wait.
- It says `Resource queue busy or an orphan requires inspection (REASON); no lock was reclaimed; …`
  only for what no running admission vouches for: a `guard` or `stranded` reason, or a slot or the
  model busy while its file names no running admission.
- When no Mac admits the work, `qk run` and `qk admit` give another Mac's reason and this Mac's last
  wait ([vocaro-engineering#270](https://github.com/vocaro/vocaro-engineering/issues/270)).

A script that runs several jobs at once leaves at least one guaranteed slot for other sessions.

## Ended and stranded owners

**An owner whose process has ended, or cannot be confirmed running, holds nothing**, and what is
left of it is stopped (owner's decision, 2026-11-13,
[vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23);
[lock reclamation](process-supervision.md#lock-reclamation)). Admission frees each such entry it
meets. Earlier rules let a stranded entry refuse every session's heavy work
([vocaro-engineering#89](https://github.com/vocaro/vocaro-engineering/issues/89)).

- **Uncertain entries.** Admission recovers an uncertain entry whose owner still runs as
  `qk admissions recover OWNER` would, once its owner has ended and a census finds nothing it ran
  still running, checking every HEAVY_RECOVER_SECONDS.
- **Stranded entries**, which recovery leaves, count as the slots they held until recovered, so only
  work beyond those waits: exclusive work's as every slot up to HEAVY_SLOTS_MAX; others' as the
  slots whose files name them, or the lowest uncounted slot; and the model if they held it.
  Exclusive work waits behind any stranded entry. A queued exclusive request holds nothing back
  while one is counted, nor a queued whole-host request while one is counted as a guaranteed slot.
- Work admitted beside a stranded entry prints `heavy: admitted beside a stranded entry: …` with
  the entry, its slots and the recover command, and its log line names them.
- `python3 scripts/qk admissions recover` lists stranded entries. This repository's
  `.claude/settings.json` lets agents run it; it refuses any entry not provably finished (owner's
  decision, 2026-11-02). No other project needs that rule, since admission recovers each provably
  finished entry itself (vocaro-engineering#315).
- What recovery leaves needs a person. `qk queue status` says when a gate is stuck behind it
  ([the merge queue](merge-queue.md#the-merge-queue)), and `qk admissions remove`, which no allow
  rule matches, removes it by hand.

## Labels

A request's label names it in waiting messages, the guard and the [admission log](#admission-log).
A lane run's label is its project's name from `queuekeeper.json` and the lane, such as
`EPUBLib gate`, so lanes of one name in several projects stay apart. A `qk admit` request's label
is its command line, and a caller that admits its own work may name it, as StudyWright's push
keeper does with `warm push REV`. A toolkit older than October 27, 2026 labels a run with the lane
alone, as in `gate`; its records are read as they are.

## Before a full gate

Look for other sessions' runs across the host, not only under the usual worktree directory:
sessions also test from checkouts elsewhere, such as `/private/tmp`. The brackets keep `grep` from
matching itself:

```sh
ps -Ao pid,args | grep -E '[x]ctest|[x]codebuild|[c]heck-all\.sh|[t]est-ios\.sh'
```

Several projects have a `check-all.sh`; its path says whose run it is. Never stop a process merely
because it is busy.

## Ask admission, not the load average

A session never waits for a Mac to go quiet by reading its load average. Work that needs a fair
run, such as a clean run of a suite after flaky ones, goes through `qk run LANE` or
`qk admit -- COMMAND`, which waits for a slot and chooses the Mac. Do not poll `uptime`,
`sysctl vm.loadavg` or `top`, or start a loop that waits for the load to fall. On these Macs the
load stays in the hundreds under ordinary work, so such a loop never fires: one would have waited
for hours while `qk run gate` passed on another Mac in 925 s (vocaro/queuekeeper#134). Work that
needs the Mac to itself asks admission for that too ([choose the request](#choose-the-request)),
and a check that must judge the host reads `QUEUEKEEPER_HOST_QUIET`
([exclusive work settles first](#exclusive-work-settles-first)). To learn why work has not started,
read [who holds and who waits](#who-holds-and-who-waits).

## Model admission

A generative-model run takes one heavy slot and the host's model lock, an `flock` on `model` in the
slot directory: both at once under the coordination lock, or neither. The hardware runs one model
workload at a time
([long-running measurements](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/shared-host.md#long-running-measurements)),
and holding the whole host would keep builds and tests waiting for no reason.

- A model request waits behind a queued exclusive request like a heavy one, and while another run
  holds the model, with a reason naming that run's label.
- Exclusive work holds the model with every slot, so no model run starts beside it.
- The guard entry and the inherited token record `"model": true`. Nested work inside model or
  exclusive admission reuses the model; a model request inside heavy admission without it is
  refused. The kernel releases the lock when the last process holding it ends.
- A lane takes the model with `"model": true` and `"resource": "heavy"`; it is one or the other of
  model and exclusive. A lane group that runs a model lane needs `"model": true` or an exclusive
  resource itself, and one that runs two or more needs `"jobs": 1`
  ([lane groups](parallel-tests.md#suites-side-by-side)).
- **Core ML whose compute units include the GPU** (`.all`, `.cpuAndGPU`) takes the model lock;
  Core ML pinned off the GPU (`.cpuAndNeuralEngine`, `.cpuOnly`) does not. A Core ML embedder
  sharing the GPU with on-device generation returned silently wrong vectors
  ([embedding correctness](https://github.com/vocaro/vocaro-engineering/blob/main/doc/knowledge/ml/embedding-correctness.md)).
- A model run that is timed or compared with a baseline stays exclusive, because it measures the
  CPU too. Whether heavy work beside generation slows it or changes its output is not measured.

## Admission log

Each Mac logs its heavy-work admissions, so that an audit can count how often each kind is taken,
how long requests wait and why, and how long each holds. Admission appends one JSON line when it
admits a request and another when that admission ends; a request that stops waiting unadmitted
appends one, as does a recovery or reclamation that removes an entry. A refused attempt and nested
work write nothing. Lines are written under the coordination lock, in the order slots were taken
and given back. A log that cannot be written never fails or holds up admission: it prints
`heavy: the admission log was not written: ERROR`.

The log is `~/Library/Application Support/queuekeeper/admissions.jsonl`, durable because audits
look back weeks and the slot directory is cleaned after three idle days
([scratch and result directories](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/shared-host.md#scratch-and-result-directories)).
A HEAVY_SLOT_DIR namespace keeps its own. A line that would take it past 8 MiB first moves it to
`admissions.1.jsonl`, replacing the previous generation. Each Mac keeps its own log.

A line holds:

- `time` (UTC, ISO 8601) and `event`: `admitted`, `ended`, `withdrawn`, `recovered` or `reclaimed`.
- `identity`, the request's nonce, which pairs an admission with its end; `pid`, the admitting
  process.
- `kind`: `heavy`, `model` or `exclusive`; `label`, up to 160 characters ([labels](#labels)).
- `slots`: the slots held, on `admitted` and `ended` lines.
- `waited`: seconds to admission or withdrawal; `held`: seconds from admission to its end, on
  `ended` lines, an exclusive settle included.
- `settled`, on exclusive work's `ended` line: the `seconds` it waited, the last `busy` share read
  (null if unreadable) and whether the Mac read `quiet` (vocaro/queuekeeper#121). Absent with
  HEAVY_QUIET_SECONDS=0.
- `holders`, `load` and `busy`, on `admitted` and `withdrawn` lines: how many admissions the guard
  recorded when the request asked, the one-minute load then, and the busy share (null until a
  reading spans five seconds).
- `wait`, on the `admitted` or `withdrawn` line of a request that was refused: `refusals` counts
  refused attempts of each kind, about one a second; `first` and `last` are the kinds of its first
  and last refusals; `load` and `busy` hold the `last` and `max` readings; `memory` is the highest
  pressure level read (1 normal, 2 warning, 4 critical). The kinds are those of
  [waiting messages](#waiting-messages): `slots`, `settle`, `cpu`, `memory`, `model`, `exclusive`,
  `wholeHost`, `stranded` and `guard`. Older lines may count `load`, or `legacy` refusals from
  StudyWright's former host lock
  ([vocaro-engineering#25](https://github.com/vocaro/vocaro-engineering/issues/25)).
- `stranded`, when the latest attempt counted a stranded entry: each entry's `identity`, `pid`,
  `label`, `uncertain`, `why`, `slots` and `model`.
- `outcome`, on an `ended` line: `released` when the command finished, whatever its exit status;
  `not-started`; `stopped` or `timed-out` when every observed descendant ended; or `uncertain`
  when the guard entry was kept for inspection
  ([lock reclamation](process-supervision.md#lock-reclamation)). On a `withdrawn` line: `timed-out`,
  which includes `qk run --if-free` declining; `stopped`; or `failed`.
- On a `recovered` line (from `qk admissions recover`, `qk admissions remove` or admission): the
  owner's `pid`, `uncertain`, the census counts `commands`, `processes` and `spaces`, `automatic`
  when admission did it, and for a removal by hand `byHand`, `unproven` and `why` from `--why`.
- On a `reclaimed` line: the entry's `identity` and `pid`, `process` (`ended` or `unknown`), `why`,
  `uncertain`, the `slots` and `model` it held, what was `signaled`, `survived` or `unsignaled`, the
  locks `replaced`, and `by`, the request that freed it, whose own line lists it as `reclaimed`.

An `admitted` line with no `ended` line is still held, or SIGKILL ended its process and a
`recovered` line follows once the entry is recovered. Lines an older toolkit wrote lack some
fields and are read as they are. The `wait` kinds say whether the limits held back work the host
could run: a wait behind `slots` is the slot counts, one behind `cpu`, `memory` or `settle` the
signals that open slots between them
([vocaro-engineering#49](https://github.com/vocaro/vocaro-engineering/issues/49)).

### Summarize the log

`qk admissions` summarizes the log over a period:

```sh
python3 scripts/qk admissions --since 2026-10-20 --until 2026-10-27
```

For each kind and label it gives requests admitted and withdrawn, their waits and what they waited
behind, the loads and busy shares seen, holds, and how admissions ended. `waited behind load 9
(30m02s)` says the load refused 9 requests and 30m02s of their waits was behind it, each wait
divided among the kinds that refused it by their refusals. A request counts in the period it was
admitted or withdrawn; a date without a zone is local; unreadable lines are counted and skipped.

To summarize another Mac's log, pass a copy, the previous generation first:

```sh
python3 scripts/qk admissions <(ssh build-host \
  'cd "Library/Application Support/queuekeeper" && cat admissions.1.jsonl admissions.jsonl 2>/dev/null')
```

## Who holds and who waits

`python3 scripts/qk admissions now` shows admission as it stands on this Mac and on each of the
farm's Macs that take work now, asked at once. It changes nothing. For each holder it shows slots,
kind and hold time; for each waiter its wait and the reason it prints itself; and for both where
the work comes from: repository, branch, worktree, agent session and Mac (`resources.work_origin`),
the sender's for work run for another Mac
([#342](https://github.com/vocaro/vocaro-engineering/issues/342)).

- **Options.** `--json` gives the same; each Mac carries `mac`, `id` (hardware UUID), `ssh` and
  `role` (its farm name, or `here`). `--here` reads only this Mac, and is required under an
  isolated `HEAVY_SLOT_DIR` or `QUEUEKEEPER_GATE_LOCK_FILE`. The readings are
  `offload_host.admissions_now` and `resources.Admission._observe`.
- **A Mac it cannot read in 10 seconds** is unknown, never idle.
- **Ended holders.** It checks the named processes with one `ps`, or PID by PID through the kernel
  when that fails or takes 5 seconds. A holder whose PID is gone or runs another process is shown
  as ended; one whose start cannot be read, as unchecked. Admission frees either at its next attempt
  (owner's decision, 2026-11-13, [vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)).
  A holder reclaimed in the last hour is listed under `reclaimed:`, never as held.
- **CPU.** `--json` gives each running holder's `cpuPercent`, from `ps`'s `%cpu`, null when unknown.
- **Progress.** Under each running holder, a `progress:` line gives its leaves' quiet-interval
  observations ([observe a quiet lane](parallel-tests.md#observe-a-quiet-lane)). Only a lane that
  declares `stall` publishes one; a holder with none is not observed, never idle or busy. A passed
  bound is evidence, and ends nothing that has not declared `"action": "stop"`
  ([vocaro/queuekeeper#70](https://github.com/vocaro/queuekeeper/issues/70)).
- **Long waits.** It does not flag a long wait: the owner deferred that until there is history to
  calibrate it. `qk queue doctor` flags one case, a queue's gate that has waited 15 minutes on a Mac
  with spare CPU, and names its holders
  ([the merge queue](merge-queue.md#the-merge-queue); [vocaro/queuekeeper#89](https://github.com/vocaro/queuekeeper/issues/89)).

## Load the queue cannot see

Admission reads the Mac before it admits and cannot account for work that never asks:

- Code-navigation (LSP) queries make `sourcekit-lsp` build the worktree at full parallelism.
  Agent sessions navigate with `grep` instead.
- Booting a fresh iOS Simulator ran the one-minute load past 400 for two minutes (October 2026).
- Xcode's indexing and an open Xcode's own builds.
- A long background measurement holds its checkout's SwiftPM build lock, so a `swift test` from a
  pre-push hook in that checkout waits silently behind it.

## Trial runs

To try Queuekeeper without reaching the live system, set `QUEUEKEEPER_TRIAL` to a folder, by its
full path, and run in a throwaway clone. Every `qk` command, and every process it starts, then keeps
its state in that folder: admission (slots, log, limits and `farm.json`), the admission guard, the
device record, kept app builds, pinned checkouts and the crash-dialog record.

- A trial's farm has no Mac, so nothing reaches another Mac, and `qk farm` refuses to change one.
- A heavy run reaps nothing first, and `qk doctor` applies none of its fixes to simulators, devices
  or another Mac's checkout, and says so.
- A queue's entries live in its repository's Git directory, so a trial's queue is its clone's own,
  and its pushes go to that clone's `origin` (vocaro/queuekeeper#34).
