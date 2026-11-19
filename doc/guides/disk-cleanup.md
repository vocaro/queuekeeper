# Reclaiming disk space

This guide covers how Queuekeeper keeps a Mac's disk and process table clean: what `qk disk` may
delete, the free-space floors that protect a run, the processes and simulators runs leave behind,
and the app registrations a Mac build leaves. Read it when the volume runs short, when `qk doctor`
lists leftovers, or before stopping anything on a shared Mac. The user guide's
[Disk space](../user_guide.md#disk-space) is the one-line summary.

## Taking the inventory

Reclaiming space needs proof that nobody's live work is in it; a quiet session, a passed deadline or
a finished-sounding name proves nothing. `qk disk` takes the inventory and changes nothing:

```sh
python3 scripts/qk disk
```

- **What it reads.** Every project in `~/Development`, whichever project runs it, and only what
  Queuekeeper and its plugins create: simulators and runtimes, DerivedData and SwiftPM caches, lane
  results kept in each Git directory, and kept app builds. Never the home directory or the rest of
  the volume.
- **What it prints.** Each item's size, the reason for its status and the exact command that would
  remove it.
- **Progress.** On stderr it names each pass and removal as it starts, and otherwise prints at most
  one line every 10 seconds. Stdout, the report or `--json`, is written line by line even to a file,
  so saved output shows how far it got ([#212](https://github.com/vocaro/queuekeeper/issues/212)).
- **What sessions leave** (linked worktrees, scratch builds under `/private/tmp`, failure records,
  evidence archives) is the toolkit's `eng disk` to remove, by the same rules
  ([reclaiming disk space](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/disk-cleanup.md#reclaiming-disk-space)).
  Run both when the volume runs short, and run `qk disk` again after `eng disk` removes worktrees,
  since their DerivedData then becomes a candidate.

## Candidates, owner items and kept items

- A **candidate** is proven unowned by the rules below, and `--delete` removes it without asking
  (the owner's decision, 2026-11-05).
- An **owner** item shows no sign of use, but only the owner can say whether it is still wanted:
  test results, a directory no repository registered, a worktree outside the session directories.
  Simulators and runtimes are never owner items, since Xcode recreates and re-downloads them (the
  owner's decision, 2026-11-03).
- Everything else is **kept**: in use, owned, of unclear ownership, or holding work or evidence.

`qk disk --dry-run` lists only the candidates, with their commands: exactly what `--delete` removes.

### In use

One `lsof` and `ps` census of the account's processes is taken first. A path is in use, and kept,
when a process's working directory, an open file or a path in its arguments lies inside it.

### Simulators

- **Pool members** of a `simulatorPool` that a main checkout names are candidates only under the
  [pruning rule](simulator-pool.md#pruning); their command is `qk simulator-pool prune`.
- **Devices of the retired**
  [disposable lifecycle](https://github.com/vocaro/apple-engineering/blob/main/guides/simulator-testing.md#disposable-simulators),
  whose names carry a UUID, are candidates when shut down, idle for the idle time, named by no
  process, and named by UDID or name in no device record (the devices module's, the one
  `QUEUEKEEPER_DEVICE_RECORD` names, and any legacy `/private/tmp` handoff file). An unreadable
  record keeps every device.
- **Xcode's default devices, unavailable devices** and device directories CoreSimulator no longer
  lists are candidates; `qk doctor` deletes this Mac's defaults itself. Any other named device may
  belong to a pool on a checkout Queuekeeper cannot see, and is kept.

### Runtimes

- **Unused.** A runtime no device uses is a candidate once simctl's `lastUsedAt` is over 14 days old
  or absent, so a pool about to create members on it keeps it. One that came with Xcode is kept.
- **Superseded builds.** When several builds of an identifier are installed, CoreSimulator uses one:
  the `chosenRuntimeBuild` of `xcrun simctl runtime match list -j`, else the build used last. Any
  other build is superseded, and a candidate once its own `lastUsedAt` is over 14 days old or
  absent, unless the toolkit pins it (the builds qualified for
  [guarded VoiceOver activation](https://github.com/vocaro/AXVerify/blob/main/guides/voiceover-simulator.md),
  `voiceover/activation.py`'s `QUALIFIED`) or a device record names it, such as a pool template's
  `runtimeBuild` ([#188](https://github.com/vocaro/vocaro-engineering/issues/188)). A template's
  `installedBuilds` pins nothing. An unreadable record keeps every superseded build.
- **Another Mac's copy.** The Macs keep the same runtimes
  ([#123](https://github.com/vocaro/vocaro-engineering/issues/123); see
  [simulator runtimes](another-mac-setup.md#simulator-runtimes)). A runtime is kept here while
  another Mac's simulators use its copy, or while that Mac's runtimes cannot be read, since removing
  it would only bring it back.
- **Never while a simulator runs.** No runtime is a candidate while any simulator on this Mac is
  running or reserved, on any runtime: they share one launch host, `SimLaunchHost`, and deleting a
  runtime can crash it ([#154](https://github.com/vocaro/queuekeeper/issues/154)). The reason names
  the simulators. The check before removal applies the same rule, so a simulator booted after the
  inventory keeps the runtimes while other categories still go.

### Build caches

- **DerivedData.** A folder in `~/Library/Developer/Xcode` (`NAME-<28 letters>`) whose `info.plist`
  names a workspace in `~/Development` or a temporary directory is a candidate when that workspace
  no longer exists. Folders for other workspaces are not listed.
- **DerivedData with no `info.plist`** is a candidate, with no process and idle for
  `--min-idle-hours`, when it holds nothing but `Logs`, `SourcePackages` and `TestResults`, or when
  its `Build` holds only `ProfileData`. Both are what an `xcodebuild` without `-derivedDataPath`
  leaves ([#158](https://github.com/vocaro/vocaro-engineering/issues/158)). Any other contents make
  it an owner item.
- **Shared folders** (`ModuleCache.noindex`, `SDKExplicitPrecompiledModules`) are kept.
- **SwiftPM's cache**, `~/Library/Caches/org.swift.swiftpm`: its clones and artifacts are
  candidates, since SwiftPM fetches them again.

### Kept lane results

Results beside the receipts in each main checkout's `.git/queuekeeper/remote/COMMIT/LANE.results/`
([a lane's results](parallel-tests.md#a-lanes-results)) are kept for each lane's newest 10 commits
and for every commit kept in the last 14 days; the rest go
([#218](https://github.com/vocaro/vocaro-engineering/issues/218)).

- A receipt is never removed, so a receipt's `keptResults` can name a folder that is gone.
- An open issue or evidence citing a commit does not keep its results: evidence copies what it
  cites ([evidence](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/evidence.md)).
- A repository with results past the rule is a candidate; its command is
  `qk remote prune-results CHECKOUT` (`--dry-run` lists them). One whose store a process uses is kept.

## Deleting

```sh
python3 scripts/qk disk --delete [--yes 'WHO ASKED']
```

- **Any session may run it** when the volume runs short, on a terminal or not, and it removes every
  candidate category without asking (the owner's decision, 2026-11-05). `--yes` only records who
  asked, and is refused without `--delete`.
- **Owner items are never removed**; their commands are only printed.
- **Each removal is checked again.** Just before it, a fresh census checks the candidate again. It
  refuses an `rm` inside a working tree or through a link, and appends every decision to
  `~/Library/Application Support/queuekeeper/disk-reclaimed.jsonl`.

### Reporting what was freed

Sizes are allocated sizes from `du`, and APFS clones share blocks, so a removal can free less than
its size. Measure free space on the same volume just before and after the run, for example with
`df -k /System/Volumes/Data`, and report the difference as the net change in free space. Label the
dry-run total as allocated size, and any per-removal measurements separately, since other activity
affects the net change.

## Free disk space

A run that fills the disk is lost, and can hang while holding the queue and the host
([#162](https://github.com/vocaro/queuekeeper/issues/162)). Two floors, in GiB free on the data
volume and on the checkout's volume, prevent it:

- **`QUEUEKEEPER_START_FREE_GB` (30).** A whole-host or exclusive lane, or one with
  `"compileFirst"`, waits for this much before asking for admission, holding no slot. It says so on
  stderr and in its wait record, which `qk queue status` shows as `waiting: free disk space: ...`,
  naming the volume, its free space and the floor. A wait longer than the lane's admission wait
  exits 75 and runs nothing.
- **`QUEUEKEEPER_RUN_FREE_GB` (2).** A command running in an admission is checked every 10 seconds.
  Once free space falls below the floor, or the admission's own write finds the disk full, it is
  stopped as a timeout stops it. A lane that failed below the floor, or could not write its log,
  receipt or record for want of space, ends the same way. Each is unmeasured, exit 3, and prints a
  line beginning `queuekeeper: out of disk space:`, on which the merge queue holds at once
  (*A run with no verdict blames nobody* in [the merge queue](merge-queue.md)).

Both are environment variables, inherited by what a process starts; 0 turns a floor off. Free
space with `qk disk --delete` and `eng disk`.

## Processes left over from runs

Supervision cannot see a child that moved to a session of its own and was orphaned between two
samples ([process supervision](process-supervision.md#process-supervision)), and such a process
was the other suspect when another Mac's disk filled repeatedly
([vocaro-engineering#45](https://github.com/vocaro/vocaro-engineering/issues/45)). So another Mac is
searched before work is sent there: `qk remote`, and `qk run` for a lane it sends, print what the
search finds before anything is pushed, and `qk doctor` lists it under `farm`, `leftovers`, for
each Mac.

A process is listed when it is the account's, runs from a checkout there
(`~/Development/queuekeeper-farm/checkouts`), the temporary directory (`$TMPDIR`) or a simulator's
device directory, and no live run accounts for it.

- **Runs from.** Its working directory, as `lsof` reports it, lies in the place, or its command
  line names a path there. Programs under `/System` are macOS's own and are not listed.
- **Accounted for.** The process or an ancestor is a live admission owner, as the guard or the
  [admission log](admission.md#admission-log) records it, or a live `offload_host.py`, which holds
  the checkout slot's lock between admissions. A recorded PID counts only with its recorded start.
- **Simulators.** A simulator's processes descend from `launchd_sim`, never from a run. A device is
  a live run's while another Mac's [device record](devices.md#device-reservations) holds it for one
  of that run's processes, or while one of them names its UDID.
- **Xcode programs and built apps.** A program such as `DTServiceHub`, or the executable of an app,
  XPC service or app extension built in a slot, is a live run's when it runs from a slot a live
  admission's owner works in and started no earlier than that admission
  (vocaro-engineering#129). Other programs from the checkout are still listed.
- **After the run.** Once the admission ends or the reservation's process is gone, these are listed.
  The search cannot tell a device the owner booted by hand from one a run left booted, and lists both.

Each leftover whose parent is not itself a leftover is listed with its PID, parent (1 once launchd
adopted it), start, command line, where it runs from, and the bytes it and each descendant have
written to files since starting (`proc_pid_rusage`'s logical writes, which need no sudo; they count
no pipe or `/dev/null`).

**The search only reports.** A failed search says why, and the work is sent all the same. Tell the
owner what it lists, with PIDs and writes: whether to stop a process is the owner's decision, as is
any other change to a shared host
([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/doc/agents/core.md#shared-resources)).
The exceptions are a process an ended run provably launched and a stale pool simulator, below.

### Leftovers an ended run launched

Routine cleanup stops a process the toolkit can prove an ended run left
([vocaro-engineering#353](https://github.com/vocaro/vocaro-engineering/issues/353)). A path, parent 1
and no claimant do not prove that: an app extension in a slot's build products also starts when
anything on the Mac previews a file.

- **The record.** While a dispatch holds its slot, it records each process launchd starts or adopts
  from the slot's bundle executables (`offload_host.Launches`, every `LAUNCH_SAMPLE_SECONDS` and as
  each command ends) in `SLOT.launched.jsonl` beside the slot's lock, with the process's and run's
  PID and start.
- **The stop.** The cleanup that reaps stale pool simulators then runs `offload_host.stop_leftovers`.
  It stops nothing while the census could not check everything, repeats every check just before the
  signal, sends SIGTERM alone, and logs each stop in `leftover-stops.jsonl` beside the admission log.
  `qk doctor` runs it as a dry run (`would stop PID …`).
- **What stays.** Each process it keeps is printed with the missing evidence
  (`did not stop PID …: …`) and stays for the owner. That includes anything started by hand or
  before this recording existed, Xcode's programs such as `DTServiceHub`, and anything in the
  temporary directory or a simulator's device.

## Stale pool simulators

Any session may run `python3 scripts/qk simulator-pool reap` without asking (the owner's decision,
2026-11-03), after a pool simulator sat booted on another Mac for 22 hours awaiting a decision.
Queuekeeper also runs it:

- in the connection that lists leftovers before work is sent to another Mac;
- on the Macs in `qk doctor`;
- once heavy work run here by `qk run` or `qk admit` is admitted, before it starts
  ([vocaro-engineering#156](https://github.com/vocaro/vocaro-engineering/issues/156)). Work a
  dispatch sent does not reap again, nor does work nested in an admission, nor a `self` lane, whose
  `qk admit` commands reap as each is admitted.

```sh
python3 scripts/qk simulator-pool reap [--dry-run] [--here] [--pool NAME]
```

With no options it acts on this Mac and then on another Mac. `--dry-run` says what it would stop and
keep, and why; `--here` leaves out another Mac; `--pool NAME` looks at one pool. It exits 1 when
anything failed, including another Mac that cannot be reached.

### When reap shuts a simulator down

It runs `xcrun simctl shutdown UDID` only when all of these hold:

1. **It is a pool's.** A member named `POOL LABEL N` of a pool a project, its main checkout here or
   its checkout on another Mac names, or that pool's warmed template,
   `POOL LABEL template (RUNTIME)` ([warmed templates](simulator-pool.md#warmed-templates); the
   owner's answer "Yes, reap them", 2026-11-04,
   [vocaro-engineering#153](https://github.com/vocaro/vocaro-engineering/issues/153)). A template's
   creator records its identity beside the simulator's data, and the template is kept while that
   creator still runs; a template-shaped name alone proves nothing for an unknown pool (#336).
2. **It is left over.** The search above lists its `launchd_sim`, and no process outside the
   simulator names its UDID.
3. **No live reservation holds it**, by name or UDID. A reservation whose recorded processes are
   gone, or whose PID has another start, is not live. One that records no process, is uncertain, or
   is not yet due counts as live, except a run's reservation (`heldOnlyWhileHolderRuns`) whose
   processes are gone and whose owner nothing accounts for: reap shuts it down and releases its
   entry in the same run (the owner's decision, 2026-11-05, vocaro-engineering#156).
4. **It has run 2 hours** (`offload_host.REAP_AFTER`), and all of this still holds at a second check
   just before the stop.

**A pool member with no live reservation goes after 2 hours, whoever booted it**, the owner
included. To keep one, boot a simulator outside the pool, or reserve it with
`qk devices reserve`, which records no process and so counts as live. A member taken with
`qk simulator-pool take` is held until an hour after the take (or `take --hold MINUTES`); to work
past that, run `qk simulator-pool extend UDID [--hold MINUTES]` or record a running process with
`devices update KEY --own PID` (vocaro-engineering#179).

**A template stays while its warm-up runs**, since the warm-up reserves it in its own name first. A
warm-up killed outright leaves the template booted with a dead holder; after 2 hours reap shuts it
down and releases it, and the next creation replaces it.

### What reap will not do

- It erases, deletes and kills nothing; other leftovers stay report-only.
- It stops nothing when anything is unread: the census, the device record, the admission guard or
  simctl's device list. A failed shutdown is reported and retried by a later run.
- Each stop is printed and logged in that Mac's
  `~/Library/Application Support/queuekeeper/simulator-reaps.jsonl` (`stopping`, then `stopped` or
  `failed`), a sibling of the [admission log](admission.md#admission-log). A log that cannot be
  written stops nothing.
- Before a dispatch or a run here, one reap spends at most 60 seconds (`REAP_SECONDS`) and starts no
  stop with under 20 left; problems are printed and the work goes ahead.
- **It cannot race a take.** `Pool.take` reserves a member under the record's lock before booting it
  and passes over booted ones, and reap's second check holds the same lock until the shutdown starts
  (at most 20 seconds; it gives up if it cannot get the lock in 5).

### Releasing dead reservations

**Reap releases a pool reservation it proves dead**, on whichever Mac the entry is, so no session
recovers leftover simulators by hand (the owner's decision, 2026-11-04,
[vocaro-engineering#156](https://github.com/vocaro/vocaro-engineering/issues/156), reversing the
report-only rule of [#97](https://github.com/vocaro/vocaro-engineering/issues/97)). Pool members are
erased before each take, so nothing on the device needs restoring. It releases an entry only when
all of these hold, in order:

1. **Its processes are gone.** The entry records a process (`holderProcess`, an active run's
   process, `--own` processes), and every one is gone from `ps` or runs with another start. An
   entry that records none, or a PID with no start to compare, stays.
2. **Its release is due, or nothing accounts for its owner** (the owner's decision, 2026-11-04).
   Early release applies only to a run's own reservation (`heldOnlyWhileHolderRuns`), never to a
   `qk simulator-pool take`, and only when no live admission or other process names its owner
   (`ownerChatID`) or the device's UDID. An unreadable admission record, or an owner shorter than 8
   characters, keeps it held.
3. **It is certain.** It is not uncertain or `restoration-unverified`, and its run did not go to
   another Mac (`run.host`).
4. **It is a pool simulator's**, listed by simctl under that key, for a pool reap looks at.
5. **simctl shows it `Shutdown`**, after this reap stopped it if it was stale, and no other held
   entry names the device.
6. **Nothing went wrong**, and under the record's lock the entry is still at the revision read.

The release is recorded as `devices recover` records one, with `recovery.by` set to `reap` and its
evidence, and logged (`releasing`, then `released`) in `simulator-reaps.jsonl` before and after the
write, so a log that cannot be written releases nothing. A released take also gets a `reap` line in
`simulator-holds.jsonl` (simulator-pool.md, Reserved simulator pool). Each release is printed
(`released KEY (UDID), held for owner …`); `--dry-run` says `would release`.

An entry that is due but not provably dead stays held, and reap names it with why and its exact
recovery command, which runs from any folder (vocaro/queuekeeper#167):

```sh
python3 ~/Development/queuekeeper/bin/qk --project ~/Development/queuekeeper devices recover …
```

For an entry in another Mac's record the command adds `--host HOST`, which runs the same recovery
through that Mac's `qk` in its login shell; a plain `ssh HOST '…'` finds the system's Python, too
old to start `qk`. If the connection fails, read the entry there again before acting.

`qk simulator-pool reap` and `qk doctor` name every such entry, and `devices status` names them too;
the reap before a dispatch or run names only those of simulators it just stopped.

## Registered Mac builds

Xcode registers a Mac app when it builds it, and macOS registers one when it launches, and neither
is undone. Stale registrations make the system run an old build's extensions, document types, URL
schemes and the like instead of the new one's
([vocaro-engineering#87](https://github.com/vocaro/vocaro-engineering/issues/87)).

So when a `qk run` or `qk admit` ends, however it ends, Queuekeeper runs `pluginkit -r` on each
`Contents/PlugIns/*.appex` and `lsregister -u` on each Mac app under the derived data its
xcodebuilds named (`app_registrations`). A lane needs no option: Queuekeeper reads the
`-derivedDataPath`, or the folder of `-xctestrun …/Build/Products/…`, of every xcodebuild among the
run's descendants every 0.5 seconds. Nested runs leave this to the outermost.

- **What counts as built.** Whatever lies in a folder the run's own xcodebuild named, up to date or
  not. Give each lane its own folder, as parallel runs need anyway.
- **What is never touched.** Anything outside the folder, including through a symbolic link; an iOS
  app; a folder another live run or outside process names, which waits; and a path that cannot be
  derived data, such as the home folder, the run's directory, a checkout or Xcode's shared default
  folder.
- **SIGKILL.** It runs no cleanup. The run's journal, in `app-registrations` beside the admission
  log, stays until the next outermost run on that Mac, in any project, finds the PID gone and
  sweeps it. A folder that had to wait, or a tool that failed, is swept the same way.
- **Not covered.** An xcodebuild with no `-derivedDataPath` (the run says so); a copy of a build
  outside its folder; a folder removed before the run ends; an xcodebuild that starts and ends
  between two samples; and registrations from before this existed.

A run reports its sweep and cleanup in one line on stderr: what it unregistered and which folders it
left registered, with reasons in the file it names. A tool's report that no such registration exists
(lsregister's `-10814`, pluginkit's `no plugin at`) counts as done. A problem here never changes
the run's result.
