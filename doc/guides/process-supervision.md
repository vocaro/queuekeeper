# Process supervision

How `qk run`, `qk admit` and the resource CLI run a command so that nothing it starts outlives it
unseen, and how a stopped or killed run's admission entry is recovered. It also covers lock
reclamation: what admission's guard, the exclusive queue and the device record hold, and how each
is freed. Read it when a run ends uncertain, when an entry holds the Mac's heavy work, or before
stopping an agent that runs admitted commands. [Process supervision](../user_guide.md#process-supervision)
in the user guide has the summary.

## Deadlines and running commands

Keep three deadlines distinct: the tool's output wait, the connection or launch timeout, and the
test's behavior or observation deadline.

- A tool timeout is an observation timeout. A tool that returns a session handle may leave the
  command running: resume the handle and inspect its log and processes rather than launching a
  duplicate.
- Wait for a confirmed terminal result before starting dependent tests or pushes.
- Never infer termination from a missing parent or an expired observation wait.

## How a command is supervised

Supervise the process group you own. The toolkit's `processes.supervise`, which `qk run`, `qk admit`
and the resource CLI use (`qk run` with a unique log per lane under `.build/queuekeeper/LANE/`),
follows these rules.

### Starting and signaling

- Start the command in a new session and process group. Capture the root's start identity
  (`ps -o lstart=`) before `poll()` can reap it; once reaped, its PID cannot seed adoption or
  group signals, even if another process reuses it.
- Signal the group and each observed descendant individually, comparing each descendant's start
  identity: Swift drivers can start other process groups and sessions.
- A refused signal never ends supervision. Darwin answers `killpg` with EPERM when the group's
  only member is the exited root that has not been reaped yet, and an unsignalable descendant is
  still awaited.
- A command that cannot start, such as a mistyped name or a script without its executable bit,
  raises `NotStarted` and releases admission. `Popen` has already reaped the child, so nothing is
  left to inspect, and keeping the guard would stop every session's admission over a typo.
- Terminate only an observed launch PID whose current executable still matches. An unknown or
  reused PID never authorizes termination, and a cleanup error keeps the first failure.

### When a status counts

A command waits for its observed descendants before its status counts. Sampling cannot prove that a
fast child did not orphan itself between samples, so:

- a confirmed terminal exit keeps its status and releases admission normally;
- a stop or a timeout whose command and observed descendants all end releases too, since its proof
  is the same as a confirmed exit's (decided 2026-10-22 for a stop, 2026-10-23 for a timeout);
- an uncertain run raises, and admission keeps its guard entry for inspection.

**The final census.** A nonzero exit, a stop or a timeout is confirmed only by a final census.

- At each sample the supervisor records the process group (from `ps`) and the session (from
  `os.getsid`, since `ps -o sess` prints 0 on macOS) of every live observed process, beside the
  root's own.
- Once the root and every observed descendant have ended, any live process in one of those groups or
  sessions leaves the run uncertain, and the error names each by PID, group and session.
- The candidates are the groups and sessions seen, never every observed PID, since PIDs wrap within
  minutes on a busy Mac. An ID that is now a live process's PID is skipped: the kernel never reuses
  a PID that still names a group or session, so that group or session had ended. An unrelated
  process that takes over a recorded ID and leaves children there reads as a survivor, which errs
  toward inspection.
- It cannot see a child that moved to a session of its own and was orphaned between two samples; on
  another Mac, the next run there lists one left in a checkout, the temporary directory or a
  simulator ([processes left over from runs](disk-cleanup.md#processes-left-over-from-runs)).
- A zero exit has no census, so a daemon a passing command starts on purpose does not stop the
  host's heavy work. Before 2026-10-26 a failed command's orphan could run on after its admission
  was released.

### The grace after the root exits

- Once the root exits, its observed descendants have 60 s to end. The supervisor lists any still
  running on stderr, as `ps -o pid,ppid,pgid,lstart,args` rows with the parent and group each had
  when first seen, and stops them: SIGTERM, then SIGKILL 10 s later, or 120 s for a foreground run,
  which restores what it changed on the Mac first ([waiting for the foreground](foreground.md#waiting-for-the-foreground)).
- The root's status counts once they are gone; a process that survives SIGKILL is still awaited. A
  lane's `timeout` bounds only the root, so a root that exits in time keeps its status through the
  grace. Xcode's `DTServiceHub` has outlived `xcodebuild … test` by 10 to 15 minutes, ignoring
  SIGTERM.
- Xcode's own programs have no grace. When every descendant still running is a program inside an
  Xcode (`…/Xcode*.app/Contents/…`, other than `xcodebuild`) that launchd adopted, the supervisor
  stops them as soon as the root exits, SIGTERM then SIGKILL 10 s later. Anything else still
  running, beside them or alone, keeps the whole 60 s. Each such grace had held a lane and its heavy
  slot about 70 s after the work ended
  ([vocaro-engineering#154](https://github.com/vocaro/vocaro-engineering/issues/154)).

### Stops and timeouts

**A stop.** `qk admit`, `qk run` and the resource CLI carry out SIGTERM, SIGINT and SIGHUP as a stop.

- A stop while queued leaves nothing to release.
- Once admitted, the job first marks its entry uncertain, in case SIGKILL follows. The supervisor
  forwards the signal to the root's group and each observed descendant, and sends SIGKILL to what
  remains 10 s later.
- When everything has ended and the census finds nothing, admission removes the entry, the
  supervisor raises `Interrupted`, and the process ends by the signal it received. If anything is
  still running 5 s after SIGKILL, or the census finds one, the entry stays uncertain for
  [recovery](#recovering-an-entry).
- A later signal does not hasten SIGKILL, because a task stop can deliver its SIGTERM twice, to the
  process group and to the process. A stop sent to the whole tree can end a sample's own `ps`,
  which is then asked again. A signal the caller ignores, as `nohup` ignores SIGHUP, stays ignored.
- SIGKILL itself cannot be caught: a killed job's entry stays, dead, for recovery. Before
  2026-10-22 none of these signals was handled, and a stopped job left its entry dead and its
  command running unsupervised.

**A timeout.** A lane's `timeout` that expires while the root runs is carried out the same way:
SIGTERM once to the root's group and each observed descendant, SIGKILL to what remains 10 s later,
and `TimedOut` once all have ended and the census finds nothing. The run is unmeasured (exit 3),
never green, and admission is released; a process still running 5 s after SIGKILL, or one the
census finds, leaves the guard uncertain.

**A nested admission.** A job admitted inside another supervised command, as when a `self` lane's
script runs `qk admit`, has a guard entry of its own, so SIGKILL waits for it to settle:

1. The outer supervisor sends the stop's signal, after a stop, a timeout or the grace, to the lane's
   processes, the nested `qk admit` and its command among them.
2. The nested job marks its entry uncertain and stops its own command, with its own 10 s to SIGKILL
   and 5 s after. It then removes its entry, or keeps it uncertain if a process is left.
3. When SIGKILL falls due and the root or an observed descendant is an admission owner (its PID and
   start are an owner's in the guard), the outer supervisor sends no SIGKILL until every such owner
   has ended, for at most `SETTLE_WAIT` (30 s) more.
4. Then SIGKILL reaches what remains. An owner that never settled is killed with its entry in place,
   for recovery.

Before 2026-10-27 SIGKILL came 10 s after the stop whatever ran, and a lane whose script admits its
own work left an entry that refused every session's heavy work until recovered by hand.

**Processes seen after the first signal.** A process first seen after the first signal of a stop,
timeout or grace receives that signal once, unless its parent is an admission owner: the owner
forwards the stop itself, and its other children are its own `ps` samples. Signaling those had
failed the owner's supervision in 4 of 144 nested stops.

### Push keepers

A push keeper ([checkout provisioning and publication](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/shared-host.md#checkout-provisioning-and-publication))
supervises its own commands and applies the same census to a command that fails on its own, with a
positive status and no stop.

- When the census finds nothing, the keeper releases its heavy-work admission but keeps the
  checkout's transaction lock, its owner record showing `"failed": true` and
  `"childrenUncertain": false`. The next push from that checkout is refused until someone has read
  why this one failed and removed the lock.
- A stop, a command ended by a signal, a killed keeper or a process the census finds leaves both in
  place, the admission entry marked uncertain, even after the observed children drain.
- A command that passes has no census.
- The keeper appends to the admission's [process record](#the-process-record) as the supervisor
  does, so `qk admissions recover` checks its entry like any other; the checkout lock stays for a
  check by hand.

### Other holders

- **Lock keepers.** A lock keeper started independently of the invoking client survives the
  client's loss and holds its locks until its command group drains. Cancellation sends SIGTERM
  without escalation, so a nonresponsive child can leave a live keeper holding the locks until
  attended recovery. Never remove a live keeper's locks.
- **UI tests.** An `xcodebuild` UI-test root and the supervisor finish at different times. Follow
  the root to a terminal test result and copy the evidence. Then shut down only the owned
  simulator, which ends its descendants sooner than the grace, and follow the supervised command
  until it exits and admission is released. Root exit, an observation timeout, a passed deadline or
  the shutdown alone does not release a reservation; when the outcome is uncertain, keep the
  reservation for recovery and launch no replacement run against it.

### Testing supervision code

Tests of supervision code bound stalls rather than durations, and start a timeout under test only
once the real descendant has published its identity ([app testing](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md)).
A bound the supervisor enforces, such as SIGKILL's wait for an admission owner, is checked at each
sample: once a sample comes after the bound, the supervisor must act before the next one. A
wall-clock limit on the whole run failed on another Mac that stalled for a minute (#314).

## Lock reclamation

Three kinds of marker coexist, and they are reclaimed differently.

**Admission's guard.** It records each owner's PID, process start, label, whether it is exclusive or
holds the model, its admission time and an uncertainty flag. Beside it, each entry's
[process record](#the-process-record) lists what supervision saw of that owner's commands.

- It is `/tmp/queuekeeper-gate-suite.lock.warm-push-guard`, named after StudyWright's former gate
  lock, or `QUEUEKEEPER_GATE_LOCK_FILE` with `.warm-push-guard` appended: StudyWright's gate checks
  an inherited admission against that path, and tests isolate admission by redirecting it.
  Admission takes no lock at the gate lock's own path, and a file left there holds nothing back.
- It is never reclaimed by time, only by PID ([below](#reclaiming-an-ended-owner)).
- Admission refuses to proceed while the guard is unreadable or not in admission's format, which it
  never overwrites ([the guard's format](#the-guards-format)).

**The exclusive queue.** `exclusive-queue.json` in the slot directory, changed only under its
`coordination.lock`, records each waiting request's PID, process start, label, `queuedAt` arrival
and `seen` last attempt.

- While its caller says during an offer that the work still waits, `seen` is when it last said so
  and `asked` the last attempt, which `qk admissions now` gives as when the place last asked; the
  next attempt removes `asked`. Ordinary records carry `ordinary`; whole-host records carry
  `wholeHost`.
- A queued request holds no slot and has started nothing, so admission drops one whose process has
  ended or whose PID has another start. A request that times out or is stopped takes itself out,
  unless the overall caller keeps its place across local waits. One whose `seen` is more than
  HEAVY_PLACE_SECONDS old holds nothing back ([heavy-work admission](admission.md#heavy-work-admission)).
- An older toolkit reads an ordinary record as an exclusive one and may hold later work back until
  the record leaves; upgrade the running toolkit copies together.

**The device record's lock.** `device-handoff.json.lock`, beside the record, is an `flock` held for
one change. The kernel releases it when its process ends, so it needs no reclamation, and its file
is never removed. A reservation in the record ends only by its owner's release or an attended
recovery ([device reservations](devices.md#device-reservations)).

### Tests and the real guard

This repository's test modules each import `tests/host_isolation.py`, which redirects the guard and
`HEAVY_SLOT_DIR` for the whole process; `test_host_isolation` fails a module that does not. A leaked
test entry once blocked the host's heavy work until removed by hand
([vocaro-engineering#88](https://github.com/vocaro/vocaro-engineering/issues/88)).

- A test that starts `qk` with an `env=` of its own builds it from `os.environ`, or from
  `host_isolation.reduced`, or passes `--unadmitted`; `test_host_isolation` fails one that does none
  of these, naming its file and line. Tests that passed only `PATH` waited on the Mac's real
  admission (vocaro/queuekeeper#45).
- `host_isolation` clears each `QUEUEKEEPER_*` and `HEAVY_*` variable a run sets for its commands,
  or lists it in `INHERITED` with why it is safe, since the suite may be one of those commands, as
  on another Mac. `test_host_isolation` plants every one the toolkit's code names and fails on one
  neither cleared nor listed; an inherited keychain variable once failed a test only on another Mac
  (vocaro/queuekeeper#46).

### Leaving admission

A job leaving admission changes only its own guard entry, whatever state the other owners are in.

- A clean exit, or a stop or timeout that saw all of its processes end, removes the entry, and the
  guard with its last one. Any other exception marks it uncertain.
- It never changes or drops another owner's entry.
- A guard that is unreadable or not in admission's format it leaves as it is, its own entry in it,
  for inspection.

Before 2026-10-22 a job that left while another owner was dead changed nothing, and the host
admitted no heavy work for 80 minutes until the guard was recovered by hand.

### Reclaiming an ended owner

When admission cannot confirm that an entry's owner still runs (its PID gone, running another
process, or its start unreadable by `ps` and then the kernel), it takes the owner as ended and frees
its slot at once, stopping whatever is left of it, so the next waiter is admitted. Uncertainty about
a holder never keeps a slot held: jobs can be rerun, and keeping the queue moving comes before
keeping any one job alive (the owner's decision, 2026-11-13,
[vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)).

Admission does it at its next attempt, on each Mac for its own guard (`resources.free_holder`):

- It sends SIGTERM, then SIGKILL 3 s later, to the owner's PID and each process its process record
  names, each only while its PID still has the recorded start, and to every process found below one
  of those by parent, each checked again by PID and start just before each signal. It never signals
  itself or its ancestors.
- A command's root that the record names before any line gives its start counts with the start it
  has now, when that start falls between the owner's and the record's last write, in whole seconds:
  the root was the owner's unreaped child then. Before 2026-11-18 such a root was not signaled and
  ran on beside the next job ([vocaro/queuekeeper#176](https://github.com/vocaro/queuekeeper/issues/176)).
  Only a command whose owner is killed before its root line is written is still missed, so a
  supervisor writes that line as soon as its command starts.
- An owner whose start cannot be read is signaled nothing, nor anything under it, since its PID is
  not proven its own.
- It then removes the entry, the guard with its last one, and its process record, and frees its
  slots, every slot for exclusive work, and the model if it held it. A lock something still holds,
  which the signals did not stop, is replaced by a fresh file, so the next admission takes it.
- The admission log's `reclaimed` line records why, what was signaled and what survived
  ([admission log](admission.md#admission-log)), and admission prints `heavy: reclaimed …`.

### Recovering an entry

An uncertain entry whose owner still runs, its stop being carried out, is recovered instead.

**Automatic recovery.** Admission recovers such an entry itself, by the checks below, before it
counts the entry against a request. It prints `heavy: Recovered LABEL (PID …): …`, logs a
`recovered` line marked `automatic` ([admission log](admission.md#admission-log)) and goes on. An
entry the checks leave is checked again after HEAVY_RECOVER_SECONDS (60), since a census reads every
process on the host; meanwhile it counts as the slots it held, every slot for exclusive work, and
the model if it held it, and a waiting message that counts it says why it stayed
([heavy-work admission](admission.md#heavy-work-admission)). Before 2026-11-01 recovery was attended
only, and entries whose commands had all ended refused heavy work until the owner removed them.

**By hand.** `qk admissions recover` carries out the same recovery. With no argument it lists each
dead or uncertain entry with its identity, label, PID, how long ago it was admitted (for an entry an
older toolkit wrote, how long ago its owner started), whether its owner still runs, and whether it
has a process record. With an owner's PID or an identity it recovers that entry:

```sh
python3 scripts/qk admissions recover
python3 scripts/qk admissions recover 56766
```

For an entry on another Mac, add its SSH name. This runs that Mac's
`~/Development/queuekeeper/bin/qk` through its login shell, whose PATH includes the Python installed
during remote setup. Its output and exit status come back unchanged. If the connection fails or
times out, list there again before acting: a lost reply does not establish whether recovery
finished.

```sh
python3 scripts/qk admissions recover --host build-host
python3 scripts/qk admissions recover --host build-host 56766
```

**The checks.** Recovery removes that entry alone, the guard with its last entry, deletes the
entry's process record and logs a `recovered` line ([admission log](admission.md#admission-log)), only
when all of these hold:

- Its owner has ended: its PID is gone, or `ps` reads another start there. A live owner's entry is
  never touched, uncertain or not: the owner removes it, or leaves it, when it ends. A `ps` that
  cannot say leaves the owner taken as running.
- It has a process record that reads whole and names the process of each command it has not
  settled, or that names no command of an owner that supervises every command it starts.
- The [final census](#when-a-status-counts), run against that record, finds nothing. It checks
  recorded processes by PID and start, and live processes in groups or sessions an unsettled command
  used. It skips an ID that is now a live process's PID, and a process whose birth follows the
  recorded close of every unsettled command that used its group or session. Without a close time,
  the space is checked without a birth cutoff.
- For a record that names no command, the owner ended before its first. Since `processes.supervise`
  records each command before starting it in a session of its own, anything earlier is in the
  owner's process group, or its session where the group leads it, and the census finds nothing
  there that started no earlier than the owner (its recorded ancestors and the leader aside). This
  errs toward keeping the entry: another lane the same shell started in that group keeps it until
  it ends. Lanes killed before their first command once left a gate waiting 86 minutes
  ([vocaro-engineering#315](https://github.com/vocaro/vocaro-engineering/issues/315)).

Otherwise it changes nothing, exits 1 and says why: each process still running, by PID with why it
counts and a `ps` listing, or what to check by hand. Stop or wait for what still runs, then run it
again. It leaves alone a guard that is unreadable or not in admission's format.

### Removing an entry by hand

An entry recovery leaves because nothing proves it finished (one with no process record, or one
whose record cannot vouch for every command) is removed by hand, once you have checked that nothing
its owner started still runs:

```sh
python3 scripts/qk admissions remove 56766 --why "ps shows nothing of BreezEPUB ways-in"
```

- It runs recovery first: an entry recovery can prove finished is recovered, and one whose owner,
  or a process its census names, still runs stays.
- It then checks what it can: each recorded process by PID and start, each live process in a group
  or session a recorded command used, and anything started since the owner in the owner's group or
  session (as the admission line records them, or else one the owner led). While any of these runs,
  it names each by PID with a `ps` listing, removes nothing and exits 1. An owner with no record
  that led no group or session leaves nothing to check, which is why the check by hand comes first.
- Otherwise it removes the entry, the guard with its last one, and its record, and logs a
  `recovered` line marked `byHand`, with why recovery left the entry and the `--why` given.

It is a command of its own, outside this repository's allow rule for `qk admissions recover`, whose
basis is that recovery removes only provably finished entries: a session's auto mode asks before it
removes one that is not (owner's decision, 2026-11-08,
[vocaro-engineering#315](https://github.com/vocaro/vocaro-engineering/issues/315)).

**An entry without a process record** is left for this check by hand: one an older toolkit wrote,
one whose record could not be written, and one whose caller runs its own commands without recording
them, as an older StudyWright push keeper does. A record that holds only its admission line, from an
unsupervised owner, counts as none. Before removing it:

- inspect its recorded owner, that owner's command processes and any reserved tree;
- stop or wait for all of the transaction's children. A push keeper's `owner.json`, in its
  checkout's transaction lock, lists each command the keeper ran with its PID, process group, start
  time and observed descendants;
- establish that an old lock was released by a free owned descriptor or a later foreign
  acquisition, checked against the recorded process birth and descriptor nonce. Time passing alone
  establishes none of this;
- preserve every foreign reservation. Delete a simulator a run left booted only by UDID, once its
  own logs prove it the run's.

### The process record

The process record is `<guard>.IDENTITY.jsonl`, beside the guard, named by the identity that keys
the entry.

**The admission line.** Admission writes a complete first line under the coordination lock before it
publishes the entry, so that no entry is published without a record, even one whose owner is killed
before its first command. If it cannot write it, the request gives up its slots before any work
starts (#336). The line holds:

- `admission`, the identity; `owner`, as `[PID, start]`; the owner's process `group` and `session`;
- the `ancestors` it was started under, as `[PID, start]`;
- `supervised`, whether the owner starts every command through `processes.supervise`, as `qk admit`,
  `qk run` and the resource CLI do (`Admission(supervised=True)`).

A toolkit older than 2026-11-08 cannot read that line, and leaves such an entry for a check by hand.
An empty or missing record from an earlier or interrupted run cannot prove that no command ran.

**Command lines.** Supervision in the admitting process (`qk admit`, `qk run`, the resource CLI, and
`processes.supervise` inside an `Admission` the same process holds) appends JSON lines, each naming
one command by a key of its own:

- `argv` before the command starts; `root`, its PID, once started;
- at each sample that sees something new, the `processes` first seen, as `[PID, start]`, and the
  process groups and sessions first seen, as `spaces`;
- `closed` updates as `[ID, epoch seconds]` when a group or session is first seen without the
  command, or `[ID, null]` when used again;
- `settled` once the run confirms that the command ended, by exiting, or by a stop or timeout that
  saw it and every observed descendant end and whose census found nothing. Recovery checks no
  settled command, so a daemon a passing command starts does not keep the entry uncertain.

Nested work in another process reuses the admission and writes nothing to the record, since the
outer supervisor sees its processes. Release deletes the record after the entry; an uncertain end
keeps it. A line that cannot be written deletes the record and ends recording, with
`heavy: the process record was not written`, so recovery never trusts a record that lacks a line.

**Callers that supervise their own commands** inside an `Admission` their process holds, as
StudyWright's push keeper does, append the same lines through `Admission.observe`, each a
dictionary: `command` and `argv` before the command starts, so a record without its `root` says it
may have started; `root`, with `spaces` holding it, as soon as it starts; each process its samples
see, with the start `ps -o lstart=` reads, and each group and session; and `settled` only once a
passing exit, or a failure whose census found nothing, confirms the end. Recovery then checks its
entry as it checks supervision's.

### The guard's format

The guard is JSON with `"vocaroEngineering": 1` and `owners`, an object of entries by identity. Each
entry has a positive integer `pid`, a `start` string, a `label` string and an `exclusive` flag, which
every toolkit has written, and any `model` or `uncertain` flag or `admitted` string it has; other
fields, which a newer toolkit may add, are read past. A guard is out of format as a whole when one
entry is, or when `owners` is missing or not an object, an empty list among them
([vocaro-engineering#72](https://github.com/vocaro/vocaro-engineering/issues/72)). Admission and
`qk admissions recover` refuse it, and nested work inside an admission it records fails rather than
reusing that admission.

## How agents' tools stop commands

What each agent action does to a running command, measured with the probes
[below](#probing-stop-signals). The rule for every agent: follow an admitted command to its terminal
result, and never stop its task to retry or to end it early.

### Claude Code

**Timeouts and a subagent's end stop nothing.** A command that outlasts its tool timeout moves to the
background and runs on, and the session is told when it ends; a subagent that ends its turn, a fork
included, leaves its background commands running (probed 2026-10-23 and 2026-10-29). A timed-out
foreground command is still the run: follow it rather than stopping it or starting another. Start
an admitted command that can outlast the foreground wait in the background. Read from the code, not
measured: a subagent run in the foreground, or one stopped, has its background commands stopped at
its end.

**A task stop.** TaskStop sends SIGTERM to every process descended from the task's shell, the command
in its own session included, and SIGKILL 1.5 s later. A command that ends promptly on SIGTERM is
released well within that. One that takes longer, as an `xcodebuild` tree can, is killed with its
admitting process and leaves its entry uncertain. To end an admitted command early, stop the
command's own processes, which supervision then follows to a terminal result, rather than the task.

**Stopping a subagent** stops every command it started, background commands included, as a task
stop does, whether from TaskStop or from the app. Subagents a coordinator stopped this way inside
`qk admit` once kept every session from heavy work for over two hours. To wind subagents down, ask
each to start nothing new, stop only its commands still waiting for admission, which hold no entry,
and let its admitted commands finish. A subagent ending at the usage limit leaves nothing behind.

**Session actions.**

- Interrupting a turn, or pressing Stop, stops only the turn's foreground command, as a task stop
  does; the session's background commands run on. One interrupt missed a child in its own session.
- Archiving or deleting a session from the sidebar stops its background commands. The app's archive
  tool refuses a session whose background command is running, but the sidebar does not.
- Quitting the Claude desktop app stops each session's background commands. Ask the owner to quit
  it only once no admitted command is running.

**Why a stop can miss a child.** Claude Code signals the shell's process group and each descendant
one `ps -A` scan lists, but abandons a scan that takes longer than 500 ms and signals the group
alone; SIGKILL follows 1.5 s after the stop begins. `qk admit` runs in the shell's process group, so
it always receives the SIGTERM and stops its command, but a command that has not ended by SIGKILL
leaves its entry uncertain and, if the scan missed it, runs on unsupervised.

### Codex

Codex does not time a command out: its shell tool returns once the requested wait passes and leaves
the command running, unsignaled, under a session handle.

- When the Codex session ends, the command is killed with SIGKILL and no SIGTERM first; its child in
  its own session runs on. An `qk admit` killed this way never marks its entry uncertain: the entry
  stays, dead, and its command runs on unsupervised.
- `codex exec` ends its session with its turn.
- A Codex Desktop thread keeps the command past its turn, listed under the thread's background
  processes. Archiving the thread ends its session; closing the app's window stops nothing.

A Codex agent therefore follows an admitted command to its terminal result before ending its turn
under `codex exec`, and before the thread is archived in Codex Desktop.

### Probing stop signals

The stops above were measured with `siglog.py` and `watch.py`, kept with the scripts that start and
read them in [`tools/stop-signals`](../../tools/stop-signals), standard library only.

- `siglog.py LOG child SECONDS` logs each SIGTERM, SIGINT and SIGHUP it receives and runs on, and
  starts a child that leads a session of its own and does the same. Each writes `exiting` after
  SECONDS. Any other signal, SIGKILL among them, ends a process without a line.
- `watch.py LOG OUT SECONDS` polls `kill(pid, 0)` every 5 ms for each process LOG names and for the
  parent of its `child`, the command's shell, and writes when each is gone. It ends when all are
  gone or after SECONDS.
- `detach.py COMMAND …` starts COMMAND in a session of its own, with launchd as its parent and its
  input and output on /dev/null, and prints its PID. A stop that signals the caller's process group
  or session, or the descendants a scan finds from its shell, does not reach it.
- `report.py DIR` reads each `NAME.log` with the `NAME.watch` beside it, and prints each probe's
  lines in seconds from the first signal logged and what ended each process.

Start the watcher detached, where the action under test cannot reach it, then have the agent or tool
under test run the probe as its command:

```sh
K=~/Development/queuekeeper/tools/stop-signals D=/private/tmp/stop-probe/NAME
mkdir -p "$D" && python3 -I "$K/detach.py" python3 -I "$K/watch.py" "$D/probe.log" "$D/probe.watch" 1800
python3 -I "$K/siglog.py" "$D/probe.log" child 300 && true    # run by the command under test
python3 -I "$K/report.py" "$D"                                # once the action is done
```

`&& true` keeps a shell given the line with `-c` as the probe's parent, as Claude Code's Bash tool
is; zsh otherwise replaces itself with a command that ends its string. Reading the report:

- a process that got a signal and went without `exiting` was killed after it, and the gap is the
  stop's escalation to SIGKILL;
- one that went with no signal logged was killed by SIGKILL alone;
- a `session-child` that ran to its own end while its `child` was signaled was missed;
- both running to their own end with no signal logged means the action stopped nothing.

A parent that outlives the probe, such as an agent's long-running process, keeps the watcher until
SECONDS.

**Driving the apps.** Computer use drives Codex Desktop (`com.openai.codex`) for these probes, under
a `mac-foreground` reservation: type into the focused composer and press Return, and use one
full-screen click to archive a thread or close its window. A stop or quit in the Claude desktop app
is the owner's to perform, since computer use only reads that app: the session starts the watcher and
the probe, the owner acts, and the session reads the report. Archive a probe's thread once it ends.

## Ending a coordinated command

Use the supplied `qk run` or resource CLI for commands. They return the child's status after leaving
admission normally. When using `Admission` directly:

- Leave the context before raising SystemExit for a nonzero result. An exception inside the context
  retains its uncertain guard, by design, because the coordinator cannot infer whether that
  exception left children running.
- `Admission` used directly handles no signals. To stop as the supplied commands do, enter
  `processes.forwarding_signals()` around it and pass `on_stop=admission.interrupted` to `supervise`.
- `supervise` in the process that holds the admission writes the [process record](#the-process-record);
  a caller that supervises its own commands writes it with `Admission.observe`, or its killed entry
  is recovered by hand. Run long-lived processes started another way through `supervise`, or record
  them, since recovery sees only what the record holds.

Legacy shell gates authenticate inherited exclusive admission for their exact lock namespace. The
coordinator does not export a lock opt-out into the suite: regression fixtures retain independent
real locks. A process wrapper must forward the admission descriptors, as the shared process
supervisor does; copying the token environment alone does not transfer ownership.
