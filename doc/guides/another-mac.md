# Another Mac

This guide is the detail behind [the build farm](../user_guide.md#the-build-farm): which Mac runs a
lane, what each Mac takes, how a run and its code reach another Mac and come back, Local Network
access, extension folders beside a project, and another Mac's own toolkit checkout. Read the user
guide's section first; read this when placement surprises you, a dispatch fails, or you change the
code that sends work.

Queuekeeper, not the session, chooses the Mac, and a session never asks the owner which Mac to use
(owner's decision, 2026-11-04, [vocaro-engineering#168](https://github.com/vocaro/vocaro-engineering/issues/168)).
Configuration calls sending work there *offloading* it. Another Mac runs work under its own
[heavy-work admission](admission.md#heavy-work-admission): each Mac's queue sees that Mac's load, so
another Mac is no private slot pool. Until 2026-10-28 another Mac was called the build host; records
from then keep that name, and configuration's earlier names are refused
([earlier names](another-mac-setup.md#earlier-names)).

```sh
python3 scripts/qk run LANE [-- ARGUMENTS]            # the Mac with spare capacity and the most headroom
python3 scripts/qk run --here LANE                    # this Mac
python3 scripts/qk run --offload-only LANE            # another Mac if it can start it now, never here
python3 scripts/qk admit -- COMMAND                   # any other heavy command, placed as a lane is
python3 scripts/qk admit --here -- COMMAND            # this Mac: a command whose products the session uses here
python3 scripts/qk remote run LANE [-- ARGUMENTS]     # any lane, on another Mac
python3 scripts/qk remote admit [--exclusive | --model] -- COMMAND
python3 scripts/qk remote passed LANE [--commit REV]
python3 scripts/qk remote setup [FORMULA ...] [--check]  # the gates' tools (Setting up another Mac)
python3 scripts/qk remote setup --runtimes [--check]     # each Mac's simulator runtimes, evened out
```

`qk remote` reaches the first Mac of [the farm](#the-farms-macs) that takes work now; `--host` names
another, by its farm name or an SSH destination. `qk remote` always means another Mac.

## Which Mac runs a lane

Every lane in queuekeeper.json, a short check as much as a gate, is placed among the Macs, this Mac
and the farm's others alike, unless it says why it stays here (owner's decisions, 2026-11-07 and
2026-11-17). The [user guide](../user_guide.md#which-mac-runs-a-lane) gives the summary; the rules
in full follow.

### Lanes that stay on this Mac

- **`"here": "REASON"`.** A lane that needs a physical device, or a measurement tied to this Mac's
  hardware such as goldens rendered on its GPU, says why it stays. Configuration validation requires a
  nonempty reason. `"buildHost"`, which once marked a lane for another Mac, is refused, true or false.
- **`self` lanes.** A self lane runs where it starts, and so do the `qk admit` commands its script runs,
  since its parts share build products. `qk run` sets `QUEUEKEEPER_SELF_LANE` to the lane's name, and each
  part's receipt says `hereBecause: a part of the self lane LANE, …`.
- **Lane groups.** A group runs its lanes under one admission on one Mac, so it stays here when it, or
  one of its lanes, says `here`.
- **Never offered.** `qk admit --here`, `qk run --here`, `--serial`, foreground work started here, and
  work nested in an admission run where they start. The receipt's `hereBecause` says why.

Simulator lanes and background Mac UI lanes go to another Mac like the rest. A lane's first run there
is its check: counts that differ from this Mac's are a defect to fix, not a reason to keep the lane
here (owner's decision, 2026-10-28).

### The farm's Macs

The owner lists the Macs that take work, on the Mac that sends it, with `qk farm` (commands in
[the user guide](../user_guide.md#adding-and-removing-macs)). The list is
`~/Library/Application Support/queuekeeper/farm.json`; nothing else names a Mac, neither an SSH alias
nor a variable. Workers started from Terminal, Codex or the signed LaunchAgent read the same file; an
explicit `HEAVY_SLOT_DIR` namespace reads its own. A farm with no Mac that takes work keeps every lane
on this Mac.

- **Adding.** `qk farm add DESTINATION` (a host name, `USER@HOST` or an `~/.ssh/config` alias) asks the
  Mac for its hardware UUID and local host name. The UUID tells Macs apart, so this Mac, or a Mac the
  farm already has under another name, is refused. Then follow
  [the setup](another-mac-setup.md#setting-up-another-mac) with `--host NAME`.
- **The old directory.** A Mac that kept its checkouts in `~/Development/build-host` has it moved to
  `~/Development/queuekeeper-farm` as it is added, its checkouts repaired, and the old path left as a
  link, since a build's own state (such as Xcode's record of a package's binary artifacts) still names
  it. The move is skipped, and `add` says so, while a run holds one of them or the new directory exists.
  A Mac moved before the link existed gets it when added again.
- **Pausing and removing.** A paused Mac is offered nothing; what runs there runs on. A removed Mac is
  gone from the list, while earlier records of it stay. `remove` refuses while a run this Mac sent there
  still has its dispatcher here, and says to pause meanwhile.

### Choosing by headroom

Each Mac that can be reached is read for its capacity, and of those with spare capacity for the work,
the one with the most *headroom* takes it: its CPU limit less its CPU's busy share. A Mac 40% busy
against a limit of 90% has 50 points; 40% against 70% has 30.

- **Ties** go by the farm's order; this Mac, and a Mac whose busy share cannot be read, come after the
  Macs the list holds.
- **This Mac chosen**: the work takes this Mac's admission and nothing is offered. A line says so:
  `queuekeeper: trying LANE on this Mac: this Mac has the most headroom of the Macs with spare
  capacity, 50 points under its CPU limit: studio 12 points under its CPU limit`.
- **Passed over**: a Mac that is unreachable or declines before the work starts; the next most
  headroom is tried. A whole-host lane passes over a Mac with a guaranteed slot busy and starts at once
  on an idle Mac later in the list.
- **No Mac with room**: the work goes to whichever admits it first, the first Mac reached asked first,
  with the Macs read again while it waits ([the offer and the wait](#the-offer-and-the-wait)).
- **A lane only some Macs may run**, such as one whose `chips` list some Macs' chips, or one a Mac's
  settings decline, is placed among the others alone ([what a Mac takes](#what-a-mac-takes)).

Each Mac's limit is its own, so the workstation's lower limit, which setup writes, makes it the later
choice at equal load, and a Mac over its limit is the last. This replaced "another Mac first, this Mac
when no other has room", which filled another Mac's guaranteed slots with one-slot work so that gates
ran here (owner's decision, 2026-11-17,
[vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211), step 3). Accepted costs: a
reading spans about 15 seconds and a gate's load dips between phases, so several placements can pick
the same quiet Mac, and work that leaves a warm checkout for more headroom builds colder. Judge a Mac's
use by its reading, not its fan.

### Spare capacity

Every Mac, this one included, is read by one test: what its own admission would admit now
([vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211), steps 2 and 3). The reading
is `offload_host.py`'s `capacity` answer, read in the Mac's login shell: its guaranteed slots and their
maximum, the busy ones, `cpuBusy` (sampled over up to two seconds when no kept sample spans one),
`busyLimit`, its load for the record, the whole-host lanes and exclusive work running, and its screen
([screen state](screen-state.md#screen-state)). A Mac has spare capacity when:

- a guaranteed slot is free (every guaranteed slot for a whole-host lane, every slot up to
  `HEAVY_SLOTS_MAX` for an exclusive one), or, for ordinary work, a slot beyond them that admission
  would open: the Mac has settled for `HEAVY_SETTLE_SECONDS` since its last admission and its memory
  pressure reads level 1, normal (vocaro-engineering#201);
- its CPU is under its own `HEAVY_BUSY_LIMIT`, or nothing admitted runs there (a Mac with nothing
  admitted has room for one job at any reading). From 2026-11-07 to 2026-11-17 a free guaranteed slot
  counted however busy the CPU; that was reversed with #211;
- no whole-host lane or exclusive work runs there;
- no whole-host request queued there within `HEAVY_PLACE_SECONDS` keeps the guaranteed slots, which
  leaves a lighter lane only a slot beyond them (vocaro-engineering#186);
- no exclusive request queued there holds later work back (vocaro-engineering#191).

The reading counts queued requests as admission does: one whose process has ended, one that has not
asked lately, one behind exclusive work, or one waiting for an uncertain guard entry whose owner still
runs keeps nothing; an entry whose owner has ended or cannot be confirmed running counts as nothing
([vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)). It runs no recovery census.
A lane's own place in this Mac's queue is not counted against it. Each refusal names its cause and
where the Mac sets the limit, for example
`studio is busy (CPU 93% busy, at or above its HEAVY_BUSY_LIMIT=90%, set in PATH) and admitted work
runs there, so it takes no more`. A reading from an older toolkit lacks some fields and withholds
nothing by them.

`qk admissions usage` and `qk queue placement` apply the same test, and so does the merge queue's wait
before a retry. `qk admissions usage` also shows the share of the last 3 hours each Mac spent at each
count of admitted jobs (`--since`, `--until`), and this Mac's admissions by why they ran here. Admission
lines record their placement from 2026-11-07 on.

### The offer and the wait

1. **Another Mac** runs the lane when its reading shows spare capacity, no Mac with room has more
   headroom, and it can start the lane now: its queue admits it within five seconds (`qk run --if-free`
   there, which exits 75 otherwise) and one of the project's four checkouts there is free.
2. **This Mac** takes the lane at once, offering nothing, when it has room and no other Mac has more
   headroom; `hereBecause` records each Mac's headroom or why it had none. A Mac whose reading showed
   room may still decline once offered, as when every checkout is in use or the model is held; this
   Mac's admission then gets the same five seconds. Busy is no configuration problem.
3. **No Mac with room**: `queuekeeper: no Mac has spare capacity for LANE now (…), so it goes to
   whichever admits it first, NAME asked first`. The lane waits in this Mac's queue, 15 s at first,
   doubling to 120 s, and between waits the Macs are read again and the lane offered again, so another
   Mac is asked at least every two minutes. A repeated offer sends no commits again. A lane that no Mac
   admits within 5,400 s ends with exit 75 (`BUSY`).

**An unreachable offer.** A Mac that cannot be reached at one offer is asked again within 30 s and
stays in rotation; only a second miss in a row counts it unreachable, and it is then asked again within
60 s after each further miss. `qk queue status` says how many probes failed and when. One slow
`ssh build-host true` once kept a reachable Mac out of rotation for six minutes
([vocaro/queuekeeper#25](https://github.com/vocaro/queuekeeper/issues/25)).

Every request keeps its arrival across retries and its place in this Mac's queue across local waits;
each wait that ends unadmitted is logged as withdrawn ([admission log](admission.md#admission-log)).

**A gate is a whole Mac's work.** A gate that runs suites side by side says `"wholeHost": true`, on the
lane or the group, takes every guaranteed slot and holds the Mac until it ends
([heavy-work admission](admission.md#heavy-work-admission), vocaro-engineering#196). A group that runs a
whole-host lane says `wholeHost` too, or is exclusive, as validation requires; a `self` lane cannot be
whole-host. Exclusive work sent to another Mac waits up to 15 minutes
(`EXCLUSIVE_PARK_SECONDS`, 900 s) for a moment it can start at once (`--if-free`), offered 15 s apart
doubling to 120 s with its first arrival, so another Mac keeps taking other work meanwhile; then it
takes its place in that Mac's queue at that arrival. Foreground work with `--exclusive` queues there
at once.

### What a Mac takes

A Mac's own settings say which kinds of work it takes and which labels it has; a lane says which labels
it needs. No lane is named in a Mac's settings, and no Mac in a lane's (owner's decision, 2026-11-17,
[vocaro/queuekeeper#211](https://github.com/vocaro/queuekeeper/issues/211), step 6). The settings are
`mac-settings.json`, beside `admission-limits.json` in `~/Library/Application Support/queuekeeper/` on
that Mac:

```json
{
  "takes": {"wholeHost": false, "foreground": true},
  "labels": ["unity-license", "apple-m5-max"]
}
```

- **Kinds.** `takes` may set `wholeHost`, `exclusive`, `model` and `foreground`. Left out, a Mac takes
  whole-host gates, exclusive work and model runs, and no foreground work. A workstation that says
  `"wholeHost": false` runs no project's gate.
- **Labels.** Lowercase words joined by hyphens, such as a license or a chip. A lane lists what it needs
  in `"labels"`, a group needs its lanes' as well as its own, and the lane runs only on a Mac that has
  every one. `here` and `chips` work as before.
- **Foreground.** A Mac whose settings say `"foreground": true` has its foreground taken for a run
  without a window agreed beforehand, and is where foreground lanes go: the first such Mac that takes
  work now and can be reached, where the lane waits for the foreground. Any other Mac is a workstation:
  foreground work started there needs a window its owner agreed
  ([attended Mac sessions](foreground.md#attended-mac-sessions)). While no reachable Mac takes it, a
  foreground lane is refused, naming the command that fixes it. `qk farm foreground NAME` writes the mark
  over SSH, `--off` removes it, and `qk farm foreground here` marks the Mac it runs on. A mark an earlier
  toolkit left in `farm.json` is read by nothing.
- **Where it is read.** Each Mac reports `takes`, `labels` and `settingsFile` with its capacity, so
  every Mac is judged alike. A Mac that declines is not offered the work, not waited for and given no
  place in its queue, and the placement line says why:
  `studio lacks the label unity-license that the work needs (its labels: none, by its settings (PATH))`.
- **When this Mac declines.** The lane runs only on the farm's other Macs and waits there; with
  `--here` it is refused with exit 2. `qk admit --exclusive` or `--model` runs on another Mac if one can
  start it now, and otherwise exits 75.
- **When no Mac takes it**, or a `here` lane's Mac declines it, nothing runs: exit 78, naming each Mac's
  reason and the fix. That is no verdict, so the merge queue holds and sends nothing back. The second
  gate of a queue's pair, kept off the first gate's Mac, ends busy instead when that Mac is the only
  other one.
- **What it does not bound.** Work nested in an admission, a self lane's parts, and a run another Mac
  sent here (placed by this Mac's reading there).
- **Reading and changing it.** `qk admissions limits` prints what the Mac takes, set or default, its
  labels and the file's path. Edit the file by hand, except the foreground mark, which
  `qk farm foreground` writes, keeping the rest. An unknown key is named as `ignored:` and passed over, so
  a later toolkit's key never stops this one. A file that is not JSON, or a known key of the wrong kind,
  stops placement on that Mac rather than guess. It is separate from the limits file because an older
  toolkit's admission refuses a limits file with an unknown key.

### Commands and where they run

**`qk admit -- COMMAND`** is placed as a lane is, at HEAD or a snapshot commit, within its `--timeout`;
`--if-free` asks each Mac once (owner's decision, 2026-11-07: all work first come, first served;
`--verify` is still accepted and means the same). It runs in the directory of another Mac's checkout
that matches the current one. It stays here, saying why, when another Mac could not run it as it runs
here:

- its directory is outside the checkout or ignored by Git;
- its words name a path of this Mac's alone: this checkout's path, a pool simulator's identifier, a
  physical device of this Mac's record, an absolute path outside the checkout (a scratchpad,
  `/private/tmp`, `/tmp`, the toolkit's checkout) or one under `~`;
- they name an existing ignored file in the checkout, such as a scratch script under `.build/issue-N/`
  (`remote.ignored_inputs`); an ignored directory, or a file written after `>` or `tee`, does not count;
- this Mac's environment sets `DEVELOPER_DIR` or any `TEST_RUNNER_` variable.

System paths (`/usr`, `/bin`, `/sbin`, `/dev`, `/opt/homebrew`, `/Applications/Xcode*.app`,
`/Library/Developer`, or what `PATH` resolves a name to) keep nothing here. A test a lane covers runs
best as that lane, `qk run LANE -- FILTER`.

**`qk admit --here -- COMMAND`** is for a command whose products the session uses here afterwards:
build output, DerivedData, a result bundle, or an app it then launches. Nothing about a command tells
such a build from a test, so the session says so. `qk app keep` builds this way.

**`qk run --here`** keeps a lane here for products the session uses here, or, once the owner decides
so, while another Mac is misconfigured. It is not for a busy Mac: a session that sees another Mac
loaded runs `qk run LANE` and asks nobody (owner's decision, 2026-11-04,
[vocaro-engineering#168](https://github.com/vocaro/vocaro-engineering/issues/168)).

**`qk run --offload-only LANE`** is for a caller that holds or will take its own admission here, such
as a push keeper running a gate under the push's admission. Another Mac takes the lane only if it can
start it now; it never runs here. Exit 75 (`BUSY`) when none is configured, reachable or free, and the
caller runs the lane itself; 78 when another Mac is misconfigured; otherwise the lane's status.

**`qk remote admit -- COMMAND`** sends a command on purpose, to run in the root of a checkout there.

### When another Mac cannot be reached or is misconfigured

Before sending anything, `qk run` checks that another Mac can be reached: its name resolves, an address
answers on its ssh port within 3 s, and `ssh HOST true` finishes within 30 s. Away from another Mac's
network, its `.local` name does not resolve and the check stops after macOS's 5 s multicast DNS wait.

- **Unreachable** is no configuration problem: `queuekeeper: running LANE on this Mac:` says why, and the
  lane runs here at once (owner's decision, 2026-10-27). The same happens when the lane's arguments name
  a physical device ([physical devices and another Mac](devices.md#physical-devices-and-another-mac)).
- **Misconfigured** stops the lane for the owner: another Mac refuses the login (an unaccepted key, a
  changed host key), its login shell fails, it cannot receive the commit or run the toolkit, it refuses
  for any reason but busy (a different Xcode at this Mac's developer directory, every checkout changed
  by a run), or it fails the project's `provision` lane. `qk run` runs nothing here, says what is wrong
  and the fix, and exits 78 (`EX_CONFIG`)
  ([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/doc/agents/core.md#shared-resources)).
  In the merge queue that is a run with no verdict, and the hold after the bound is the owner's; a
  session that finds the cause gone still resumes it ([the merge queue](merge-queue.md#the-merge-queue)).
- **A Mac that cannot answer with a reading** runs the lane here, saying so; one whose admission limits
  cannot be read is misconfigured.
- **Lost after sending**: the run is unmeasured rather than run a second time here, since it may still
  run there.

A lane that runs here after another Mac declined says so before it starts:
`queuekeeper: gate runs on this Mac (NAME), not another Mac: REASON; another Mac's lines above are from
the offer it did not take`. Messages name a farm Mac by its name in `qk farm list`.

### Check gate placement without running a gate

Run `python3 scripts/qk queue placement` from the project. It reads the first gate lane on
`origin/main`, lists the farm's Macs in order, and reads each one's capacity over SSH. Give a lane name
to inspect another lane, or `--json` to save timestamped readings. It changes no entries, workers or
admissions.

- Read the refusal beside the slot counts. With one of three guaranteed slots occupied, a `wholeHost`
  gate waits for the last one. A queued exclusive request holds back later work while admissions drain,
  and when it starts can take every slot up to the maximum.
- The same PID and process start in two readings identify the same blocker; the admission identity
  matches a holder to its release in that Mac's admission log.
- A reading is a snapshot: the Mac checks admission again when the lane starts. Use `qk queue status`
  for a gate already waiting and `qk admissions usage` for history.

[Cloud gate runners](cloud-gate-runners.md) compares overflow hosts and the cost of a pilot.

How a locked screen affects placement is in [Screen state](screen-state.md#screen-state).

## Simulators on another Mac

A lane that boots simulators runs on another Mac as it does here: headless, from a session started
over SSH, from that Mac's own pool, whatever its screen's state. Its first run there is its check
(owner's decision, 2026-10-28; measured on 2026-10-27 with matching test counts,
[vocaro-engineering#31](https://github.com/vocaro/vocaro-engineering/issues/31)). A lane that also
needs a physical device stays here.

- **Each Mac has its own [pool](simulator-pool.md#reserved-simulator-pool) and
  [device record](devices.md#device-reservations).** A member of the same name on two Macs is two
  simulators, and a reservation on one says nothing of the other. Physical devices are reserved only in
  this Mac's record ([physical devices and another Mac](devices.md#physical-devices-and-another-mac)).
- **Owners.** `QUEUEKEEPER_DEVICE_OWNER` does not cross SSH, so a reservation there names a generated
  owner: `simulator-tests-UUID` for `qk simulator-tests`, `POOL-UUID` for a gate using
  `queuekeeper_apple.simulators.pool.member()`.
- **First run.** A project's first run there creates its first members and lets their first boot
  finish, which can load the Mac heavily for a few minutes.
- `qk simulator-pool list` and `qk disk` show the pool of the Mac they run on; a run there prunes that
  Mac's members of the kind it takes.
- **What comes back.** Only logs and receipts. `qk simulator-tests` leaves each device's log and result
  bundle in the checkout there, named in the lane's log, with a failing device's last 40 lines in it. To
  get a result bundle or device log here, run the lane with `--here`.

## How a run reaches another Mac

`qk run`, `qk admit`, `qk remote run` and `qk remote admit` all send work the same way:
a commit, pushed over SSH to bare repositories under `~/Development/queuekeeper-farm` there, so nothing
reaches GitHub. `qk remote` takes no slot here.

### The commit sent

- **The project.** HEAD when the working tree is HEAD's; otherwise a snapshot commit of the working
  tree: tracked changes, deletions and nonignored untracked files, staged through a copy of the index
  (`GIT_INDEX_FILE`), written with `git write-tree` and `git commit-tree`, HEAD its parent. The session's
  index, HEAD, branch and tree are untouched and no ref holds the snapshot. The index copy keeps the
  original's modification time, or Git trusts stale stats for a file rewritten at its size in the same
  second (2026-10-27, [vocaro-engineering#56](https://github.com/vocaro/vocaro-engineering/issues/56)).
- **Ignored files stay out.** A large untracked file that no `.gitignore` names is sent and stays among
  the repository's objects until garbage collection, so ignore what another Mac does not need.
- **The toolkit** goes the same way, as a snapshot when it has changes, rather than keeping the work
  here (owner's decision, 2026-11-02,
  [vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)). A toolkit checkout
  that is clean, on `main` and only behind its last-fetched `origin/main` sends `origin/main`'s commit
  instead, saying so in one line, because the shared checkout waits behind while a process runs from it
  ([merge-queue.md](merge-queue.md), **The shared checkout follows each toolkit landing**) and a change
  needing a just-landed toolkit API failed until it followed (owner's decision, 2026-11-07,
  [vocaro-engineering#227](https://github.com/vocaro/vocaro-engineering/issues/227)). A toolkit sent as
  its own tree while behind `origin/main` is named in one line. A queue's gate uses a clean copy of the
  toolkit instead ([which toolkit a gate runs](merge-queue.md#the-merge-queue)).
- **A snapshot's result is no commit's pass.** Its receipt stays in `.build/queuekeeper/remote/LANE/` and
  `qk remote passed` never counts it. `qk run` and `qk remote run` say so as they send one, naming the
  changed files ([vocaro-engineering#197](https://github.com/vocaro/vocaro-engineering/issues/197)).
  Commit and run again for a pass that counts.

### Checkouts there

`offload_host.py`, sent along and run by the Mac's own Python, takes one of four persistent checkouts
of the project, checks the commit out detached with the toolkit beside it, and runs `qk run` (or the
command) there. The connection that prepares it first lists processes earlier runs left running
([processes left over from runs](disk-cleanup.md#processes-left-over-from-runs)).

- **Warm by nearness.** Of the free checkouts, a run takes the one whose last tree differs from its
  commit in the fewest files, and the log says which (`slot 1: its last tree is the commit's, so its
  builds are warm`). Uncomparable or empty slots come last, in index order. In index order alone a
  gate's first step once took twice as long on a cold slot (2026-11-05,
  [vocaro-engineering#183](https://github.com/vocaro/vocaro-engineering/issues/183)).
- **All in use**: `qk remote` waits for one.
- **Provision first.** A project's `provision` lane runs first, as `qk run --here provision`, admitted
  as every lane is, with `--if-free` when the work was offered to start now; the work runs only if it
  passes.
- **No bytecode.** The work runs with `PYTHONDONTWRITEBYTECODE=1`, so a project's `.gitignore` need not
  name `__pycache__`.
- **Changed checkouts are passed over** and kept for inspection. While none is free and some are
  passed over, the wait or decline names the changed files rather than saying all are in use
  ([vocaro/queuekeeper#44](https://github.com/vocaro/queuekeeper/issues/44)). Once read, remove one with
  `git --git-dir ~/Development/queuekeeper-farm/repositories/NAME.git worktree remove --force PATH`.
- **Knowing it runs there.** The work finds `QUEUEKEEPER_DISPATCHED_FROM` set to this Mac's name; test
  only that it is set
  ([asking whether a command runs on another Mac](../user_guide.md#asking-whether-a-command-runs-on-another-mac)),
  never a host name, user ID or device-record field, which macOS and setup can change
  (vocaro/queuekeeper#93).

### A named checkout slot

`qk remote run LANE --checkout-slot N` and `qk remote admit --checkout-slot N -- COMMAND` run in
checkout N, 0 to 3, and no other, for a check that depends on the checkout's absolute path, warm
products or generated project. Repeat one across slots by naming each:

```sh
for slot in 0 1 2 3; do python3 scripts/qk remote run LANE --checkout-slot "$slot"; done
```

- The run waits for that slot under its lock even while others are free, and names the holder. The
  waiter has no place in line.
- The wait is bounded by `--checkout-wait SECONDS`, else 5400 s; running out exits 75, having run
  nothing.
- A number outside 0 to 3 is refused before anything is sent. A named slot that a run left changed is
  refused with what removes the changes.
- The receipt's `checkoutSlot` gives `requested`, `assigned`, `path` and `waitSeconds`.

A checkout slot says where a run's files are. It is neither a heavy-work slot nor the foreground
reservation, both taken later, so waiting for a checkout holds neither.

### Pushes

- **Warm pushes.** A push learns what another Mac has only from advertised refs, so each repository
  there names each commit its checkouts hold as `refs/queuekeeper/checkouts/COMMIT`, renamed before
  every push. A push then sends only what those commits lack. Without them a one-line change sent the
  whole history (2026-10-29,
  [vocaro-engineering#67](https://github.com/vocaro/vocaro-engineering/issues/67)). A push is cold when
  this Mac has none of the named commits.
- **Races.** Two dispatches pushing one commit race for its ref, and the loser gets
  `cannot lock ref …: reference already exists`. Queuekeeper then asks with `git ls-remote` for both of
  the commit's names; if either holds it, it says `holds COMMIT all the same` and goes on. If neither
  does, the push is made again, three pushes in all, half a second and then a second apart
  ([vocaro-engineering#152](https://github.com/vocaro/vocaro-engineering/issues/152)). When each loses,
  the lane stops naming the race: send it again, or start such dispatches a few seconds apart. Any other
  failure is not retried. The push record gives `race`, `attempts` and `raced`.
- **No Git settings.** `push.negotiate=true` needs protocol v2, which a stock macOS sshd does not pass
  (`AcceptEnv` takes only `LANG` and `LC_*`); queuekeeper sets no Git setting, since the refs make a push
  warm over any protocol.
- **A 30-minute bound.** A push not finished in 30 minutes, or interrupted by a stop, is ended with its
  ssh, and nothing of it enters the repository; the work stops for the owner as for a failed push. A
  leftover `objects/tmp_objdir-incoming-*` quarantine there is no part of the repository and is pruned
  after `gc.pruneExpire`.
- **The record.** The receipt's `preparation` gives the seconds of staging, the preparing connection and
  each push, each Mac's `git --version`, and per push its status, phases (`connect`, `pack`,
  `transfer`, `receiver`), objects and bytes, warm or cold, and any extra lines Git wrote. The result's
  `phases` give the wait for a checkout and the placing; the work's receipt gives `queueSeconds`.

### The size of the ssh command

`offload_host.py` travels compressed in the ssh command itself, since it runs before another Mac has
the toolkit, and OpenSSH bounds a packet at 256 KiB. A plan, and a reconnection, are sent only the
functions they run, with blank lines where the rest stood so that tracebacks keep the file's line
numbers (`remote.plan_program`); a question, such as capacity or screen, gets the whole file. A test
holds a plan's command under two thirds of its 128 KiB and a question's under 192 KiB
([vocaro/queuekeeper#216](https://github.com/vocaro/queuekeeper/issues/216)). A part with a request of
its own that needs nothing of the file, such as the holder of
[a waiting gate's place](admission.md#heavy-work-admission), is a file of its own (`place_holder.py`).

### What comes back

- **Output** streams back as it is written. The log, output and receipt land in
  `.build/queuekeeper/remote/LANE/` (`remote/admit/` for a command). A group's lanes' logs and receipts
  follow into `lanes/` once the run ends, a stopped run's included, listed in `returned`; stale ones
  there are removed first.
- **Exit status** is another Mac's: the work's, or 1 for a run stopped before the work began, even after
  the provision lane passed (vocaro-engineering#317).
- **Each dispatch's own copy** is kept in `.build/queuekeeper/remote/.dispatches/LANE/TIME-PID/`; `LANE/`
  holds the one that ended last. The summary and extension results are read from the run's own
  dispatch, never `LANE/` ([vocaro/queuekeeper#9](https://github.com/vocaro/queuekeeper/issues/9)). Each
  name keeps its newest eight, and dispatches of one name file their results in turn
  ([vocaro-engineering#67](https://github.com/vocaro/vocaro-engineering/issues/67)).
- **Comparable Macs.** The receipt names both Macs' model, macOS and Xcode. Another Mac must have the
  same Xcode build at this Mac's developer directory, or the run is refused; a different macOS is
  reported but refuses nothing.
- **Passes by commit.** A whole lane's receipt is kept by commit in the repository's Git directory, so
  `qk remote passed LANE` answers from any worktree whether the lane passed at exactly that commit. A
  whole lane run here at a clean commit by a clean toolkit is kept too, with `ranOn` `this Mac`, so a
  gate the merge queue ran here counts
  ([vocaro-engineering#198](https://github.com/vocaro/vocaro-engineering/issues/198)). A later run at
  that commit replaces it; its `results/` are kept beside it as `LANE.results/`
  ([a lane's results](parallel-tests.md#a-lanes-results)).
- **The receipt's commits.** `commit` and `head` (differing only when `snapshot` is true), and for the
  toolkit `toolkit`, `toolkitHead`, `toolkitSnapshot` and `toolkitCheckoutHead`.
- **Where it ran.** `ranOn` is `this Mac` or the farm Mac's name (`second Mac` before 2026-11-16,
  `build host` before 2026-10-28), `mac` names the Mac, and a farm run adds `host`, `farmMac` and
  `macId`, the hardware UUID. `hereBecause` says why a run ran here. Another Mac's own receipt, nested
  inside, names the sender in `dispatchedFrom`. `qk admissions` on each Mac's log counts its admissions
  by lane ([admission log](admission.md#admission-log)).
- **Serial runs.** `qk remote run --serial GROUP` runs a group's lanes one after another there, its
  serial reference
  ([parallel-only failures](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/parallel-tests.md#a-failure-seen-only-side-by-side));
  its receipt stays in `remote/GROUP/` and is no pass of the group.
- **A command's edits.** What a command sent with `qk admit` or `qk remote admit` changed in files Git
  does not ignore comes back as `changes.patch` and is applied here with `git apply --binary`, to the
  working tree only, while the tree here is still the one sent
  ([#80](https://github.com/vocaro/queuekeeper/issues/80)). Otherwise nothing is applied, the patch is
  kept as `.build/queuekeeper/remote/unapplied/DISPATCH.patch`, and the command exits 73 (`UNAPPLIED`);
  apply it once the tree allows, or run again. Ignored files, such as build products, stay there, and
  `qk admit` says so, naming `qk admit --here`. A lane brings back no patch.

### Environment variables do not travel

Another Mac runs work in its own login shell's environment, so a variable set for `qk run` or
`qk admit` here does not reach it, and the loss is silent when a script has a default (a lane given its
runtime in a variable once ran on the default one). Pass a lane setting as an argument after `--`
(`qk run purchases -- --runtime ID`) and confirm it in the receipt, or use `--here`. What a project's
extensions add to every command's environment (`extensions.COMMAND_ENVIRONMENT`) does reach it: each
Mac's `qk` sets it from its own checkout of the extension's folder
([vocaro/queuekeeper#97](https://github.com/vocaro/queuekeeper/issues/97)).

### Running here after an offer

A lane this Mac takes runs over this checkout's working tree once admitted, not the commit sent, only
while the tree is still that commit's tree.

- **The project changed meanwhile**: `LANE runs on this Mac from what was sent, COMMIT, in a temporary
  worktree`, with the changed paths. The worktree holds the commit sent, the toolkit beside it and
  `QUEUEKEEPER_ROOT` naming it; `provision` runs there first. It is removed when the lane ends, however
  it ends, with everything written there, so builds start cold; only `QUEUEKEEPER_RESULTS_DIR` is kept,
  in `.build/queuekeeper/LANE`, with `provision.log`. The receipt's `sent` and `sentTree` say what ran.
  The lane used to refuse with exit 2 over an unrelated edit
  ([vocaro-engineering#79](https://github.com/vocaro/vocaro-engineering/issues/79),
  [vocaro/queuekeeper#136](https://github.com/vocaro/queuekeeper/issues/136)). When no worktree can be
  made, the lane exits 2.
- **Only the toolkit changed**: `LANE runs on this Mac with the toolkit that was sent, COMMIT`, in this
  checkout with a temporary toolkit worktree; the receipt's `sentToolkit` records it
  ([vocaro/queuekeeper#33](https://github.com/vocaro/queuekeeper/issues/33)). The `qk run` process keeps
  its own code, and a project reaching the toolkit by relative path rather than `scripts/qk` still
  reads the checkout.
- **`qk admit`** does the same
  ([vocaro-engineering#276](https://github.com/vocaro/vocaro-engineering/issues/276)), its provision log
  in `.build/queuekeeper/admit/provision.log`; its edits come back as from another Mac, usually as a
  kept patch, exit 73. Files a tool writes outside the worktree, such as default DerivedData, stay.

**Provision here.** A lane that runs here, by `--here`, its own `here` or because no other Mac had room,
runs after `qk run --here provision` in the same checkout, inside the lane's admission. A failing
provision lane exits 78, naming `.build/queuekeeper/provision/run.log`: no verdict, so the merge queue
holds ([vocaro/queuekeeper#19](https://github.com/vocaro/queuekeeper/issues/19)). The provision lane
itself, nested work and work another Mac sent run none.

**One run per results directory.** A run of a lane started here while another run of it from the same
checkout is still running keeps its files in `.build/queuekeeper/.runs/LANE/TIME-PID/`, newest eight
kept ([vocaro/queuekeeper#9](https://github.com/vocaro/queuekeeper/issues/9)). Every `qk run` ends with a
summary on stderr: verdict, test counts, the Mac's load, failing tests and the path to `failures.txt` or
the receipt (`lane_report`); a run on another Mac leaves it to the sender
([receipts](lanes.md#receipts-and-test-counts),
[vocaro/queuekeeper#133](https://github.com/vocaro/queuekeeper/issues/133)).

### Stops, sleep and lost connections

- **A stop carries over**: another Mac stops the run as a stop signal stops a local one, and it leaves
  admission. A connection's end is no stop
  ([vocaro-engineering#321](https://github.com/vocaro/vocaro-engineering/issues/321)).
- **Sleep or a dropped connection**: a started run goes on; `qk remote` reconnects after each failure and
  collects the log, result and returned files from the journal another Mac keeps under `dispatches/`.
  The receipt's `connection` says so. A stop asked for meanwhile reaches it on the next connection.
- **Exceptions** end with the connection: a run carrying a device reservation, whose record is this
  Mac's ([physical devices](devices.md#physical-devices-and-another-mac)), and one whose dispatcher had
  not yet heard it start. For these the dispatcher writes a heartbeat every 20 s and another Mac stops
  the run after 180 s of silence (`remote.HEARTBEAT_SECONDS`, `HEARTBEAT_SILENCE`), longer than ssh's
  two-minute bound (vocaro-engineering#287, #321).
- **A dispatcher that died.** Another Mac cannot tell it from sleep, so this Mac decides: a dispatch
  records its journal under `dispatchers/` (`remote.dispatch_records`), and the next dispatch or worker
  start sends `stop` through `reattach` for any whose dispatcher is proven gone. The journal is kept a
  week. A live dispatcher, as on a sleeping Mac, is left alone.
- **Nobody attending.** Past the lane's timeouts, or `--time-limit`, plus 95 minutes for a checkout and
  admission, another Mac stops what is left of a run whose connection was lost, SIGKILL five minutes
  later; the result says `detached` and `overran`. A run that returns no result within that bound of
  this Mac's waking is unmeasured, never passed or failed.

### Reading a connection failure

ssh closes a connection silent for about two minutes (`ServerAliveInterval` 30 s, four times), so a
shorter silence leaves the run going. A failed connection reports what ssh said, when the run was
dispatched and lost (UTC), and how long this Mac slept between, from a clock that runs through sleep
against one that stops. A Mac asleep runs no ssh, so a timeout this Mac slept through says nothing about
another Mac, and queuekeeper then says to keep this Mac awake (2026-10-26,
[vocaro-engineering#44](https://github.com/vocaro/vocaro-engineering/issues/44)).

| ssh says | Meaning |
| --- | --- |
| `ssh: connect to host ADDRESS port 22: Operation timed out` | nothing answered TCP within `ConnectTimeout`'s 10 s |
| `Connection timed out during banner exchange` | TCP answered, no SSH banner within 10 s |
| `Connection to ADDRESS port 22 timed out` (no banner line) | the banner came, then silence for two minutes during key exchange or authentication |

### Keeping the Macs alike

`qk doctor` compares the Macs and reports a difference as an error: their default Xcodes, the Xcode at
this Mac's developer directory (which every run there uses), and the simulator runtimes, as well as a
Mac that answers but refuses the login. An unreachable Mac is `unchecked`, not an error, since work then
runs here (owner's decision, 2026-10-28). A different default Xcode still runs lanes, but anything that
reads the default there sees another Xcode.

- **Runtimes** are doctor's to even out, not the owner's (owner's decision, 2026-11-03,
  [vocaro-engineering#123](https://github.com/vocaro/vocaro-engineering/issues/123)), as
  `qk remote setup --runtimes` does; the difference is an error only when that fails.
- **Stale pool simulators** are shut down on each Mac that answered, as `qk simulator-pool reap` does,
  listed under `farm`, `reaped` ([stale pool simulators](disk-cleanup.md#stale-pool-simulators),
  [vocaro-engineering#127](https://github.com/vocaro/vocaro-engineering/issues/127)).
- **The report's `farm`** has one comparison per Mac that takes work now, with `host`, `name`,
  `unchecked` and `leftovers` ([processes left over from runs](disk-cleanup.md#processes-left-over-from-runs)).
- **`connection`** gives the name and port `ssh -G` gives and each resolved address in ssh's order with
  its connect time, up to the first that answers. Leading addresses that do not answer are an error,
  naming the `AddressFamily` line that skips them when they are all of one family
  ([setting up another Mac](another-mac-setup.md#setting-up-another-mac)). An unresolved name is in
  `unresolved`; a Mac reached through a proxy is not measured.

## Local Network access

macOS's Local Network privacy judges a connection by the process macOS holds responsible, not the
process itself. The Claude app's `disclaimer` helper makes each session's `claude` responsible for
everything the session starts, the detached queue worker included. Once that `claude` exits, as when its
session is archived, macOS holds each such Python responsible for itself, and every local-network
connection it makes fails at once with `EHOSTUNREACH`, `No route to host`, while other sessions still
reach another Mac (vocaro-engineering#282). A launchd agent running Python is blocked the same way.

- **Detected, not misreported.** When a connection to another Mac fails, the reach check asks macOS
  which process is responsible (`responsibility_get_pid_responsible_for_pid`); when it is a Python, it
  raises `NoLocalNetwork` rather than reporting the Mac down or searching for a renamed one.
- **The queue worker** asks before each round while another Mac is configured, and on that answer exits,
  holding nothing, its entries back in the queue. The next `qk land`, `qk queue wait` or
  `qk queue resume` from a live session starts a worker that can reach it; a session that finds the
  queue waiting with no worker runs `qk queue resume`.
- **During a gate**, the worker's `qk run` for a gate's lane that gets `NoLocalNetwork` runs nothing here
  and exits 69 (`EX_UNAVAILABLE`); the worker treats it as no verdict, not counted toward
  `queue.noVerdictRuns` (vocaro-engineering#300). A session's own `qk run` keeps its fallback to this Mac.
- **The fix** is the [signed queue launcher](maintenance.md#signed-queue-launcher): a LaunchAgent runs a
  Developer ID-signed app that names its bundle ID in `AssociatedBundleIdentifiers` and stays alive
  while its worker runs; in testing it kept connecting after its starter exited (vocaro-engineering#299).
  See Apple's
  [TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).
- **Tests** never ask macOS: `QUEUEKEEPER_RESPONSIBLE_PROCESS` gives the answer instead (`PID PATH`, or
  other text for no answer), set for every `qk` the suite starts (`tests/host_isolation.py`), so results
  do not depend on which process tree runs the suite
  ([vocaro/queuekeeper#220](https://github.com/vocaro/queuekeeper/issues/220)). No run sets it.

## Extension folders beside a project

A project's extension may load from a folder beside it, named in `queuekeeper.json` as
`{"module": NAME, "path": "../FOLDER/..."}`. Other projects share that folder and landings move it, so
whatever runs lanes pins it at one commit when it starts, and every lane of that run, on any Mac, and
every nested `qk` command loads that commit. Unpinned, a landing mid-run once reached later lanes with
an adapter that no longer matched (vocaro-engineering#497).

**Folders such a folder loads from.** A folder's own `queuekeeper.json` may list under `siblings`
further folders its code loads from, with the variable it finds each by:
`"siblings": {"apple-engineering": "APPLE_ENGINEERING_ROOT"}`. A run pins each listed folder by the same
rules and sets the variable to its checkout in every command, on any Mac. A project may list `siblings`
for its own code too. A listed folder a Mac has no checkout of is left out there.

**Which commit.** The folder's `origin/main` as this Mac's checkout names it: fetched by the queue
worker each round, taken as it is by `qk run`. The checkout's HEAD, branch and changes are never read.
`qk run` names the commit as it starts, and the receipt holds it under `siblings`.

**Which commands pin.** The queue worker's round, `qk run`, `qk remote run`, and a caller that sets the
pin set, as `eng checkpoint` does. Other commands (`qk --version`, `qk unittest`, `qk doctor`,
`qk admit`) load the folder as it stands. The pin set travels in `QUEUEKEEPER_SIBLINGS`; a command that
finds it set uses it, and the queue's worker drops an inherited one and makes its own.

**Where the commit is checked out.** Once per Mac, in
`~/Development/queuekeeper-pins/FOLDER/COMMIT/FOLDER` (ending in the folder's name because SwiftPM names
a local package by its folder): a read-only detached worktree, shared by runs pinned to that commit and
never moved. `qk disk` lists them, and `qk disk --prune-pins` removes each unused for a week.

**Another Mac** is sent the commits only. It takes each from its own `~/Development/FOLDER`, fetching
when it lacks one, and links the pinned checkout beside the dispatched one. The preparing connection
fast-forwards `~/Development/FOLDER` there under the same conditions as
[another Mac's own toolkit checkout](#another-macs-own-toolkit-checkout). On this Mac, a project whose
`main` sets `"queue": {"followLandings": true}` has its main checkout fast-forwarded after each landing.

**A pin set that does not start.** Before a round adopts a new set, the worker starts Queuekeeper with
it in the project at `origin/main`. If that exits 70, the round uses the last set that started and the
log names the folder that moved; with no earlier set, the queue holds for a session.

**A commit another Mac cannot take.** When it has no checkout of the folder or cannot fetch the commit,
the lane ends before its command with exit 70 and `queuekeeper: could not start:`, naming the folder,
commit and git's error. The queue holds as for any lane Queuekeeper could not start for
([the merge queue](merge-queue.md#the-merge-queue)); the cause is that Mac's checkout, network or
credentials, for the owner.

**Testing a newer commit of the folder.** `qk run --sibling FOLDER=WHERE LANE` pins the folder at a
commit of this Mac's checkout or at a checkout's path, as its tree stands; the receipt records it. A
commit no branch of the origin holds runs on this Mac only; push it to use another Mac. A command
already under a pin set refuses `--sibling` with exit 2, so no merge-queue lane chooses what it is
judged with. A project change needing an unlanded folder change is two landings, the folder's first.

A folder that is no Git checkout, or names no `origin/main`, loads as it stands.

**Another Mac needs its own checkout** of each such folder before the extension is listed:
`git clone` the origin this Mac's checkout names into `~/Development/FOLDER` there. Without it every
lane sent there ends as one Queuekeeper could not start for (vocaro/queuekeeper#117). `qk doctor` and
`qk remote setup` check for it and name the clone; making it stays the owner's step.

**Test fixtures** should map each extension to its own checkout, not rewrite every `path` to the
toolkit's (vocaro/queuekeeper#117).

## Another Mac's own toolkit checkout

Another Mac has a toolkit checkout at `~/Development/queuekeeper`. No dispatched run uses it, but
anything run straight from it does, such as a [recovery](disk-cleanup.md#stale-pool-simulators) over
ssh. Queuekeeper keeps it current (owner's decision, 2026-11-05;
[vocaro-engineering#203](https://github.com/vocaro/vocaro-engineering/issues/203)), in two places:

- the connection that prepares each dispatch, passing the commit this Mac's `origin/main` names; a
  checkout already holding it counts as current without a fetch;
- `qk doctor`, in what it fixes on the Macs (`cli.fix_hosts`), which always fetches.

It fast-forwards to `origin/main`, without hooks, under the same conditions as this Mac's shared checkout
([merge-queue.md](merge-queue.md), **The shared checkout follows each toolkit landing**):

- it is on `main`;
- it has no changed, staged or untracked path (ignored files stay);
- it holds no commit `origin/main` lacks;
- it is behind; and
- no process of the account runs from its tree (a working directory or argument below it, outside
  session worktrees and Git's own directory; open files are not read).

Otherwise it is left as it is: a dispatch prints
`remote: left HOST's toolkit checkout ~/Development/queuekeeper at COMMIT as it is: REASON` and goes on,
and `qk doctor` reports the same as an error. These cases are the owner's; a session does not stash,
reset or edit that checkout. An update prints
`remote: updated HOST's toolkit checkout … from OLD to NEW, N commits of origin/main`.

- **Time.** One 40 s deadline for every step, the fetch at most 25 s; each git step runs in its own
  process group, which the deadline kills.
- **No prompts.** `GIT_TERMINAL_PROMPT=0` and ssh in BatchMode; the fetch uses
  `~/Development/queuekeeper-farm/git-credentials` when it exists.
- **Concurrency.** A lock under `~/Development/queuekeeper-farm` lets one connection update at a time;
  others skip.
- **Failures** become the reason and never fail the dispatch.

**Tests never reach a real Mac.** `tests/host_isolation.py` replaces `cli.fix_hosts`, and a test's farm
is its own namespace's, empty unless the test writes a fake Mac. Stand-ins for `ssh` and `dns-sd` come
first on `PATH` for the suite and everything it starts, so every connection fails as to an unresolvable
name and Bonjour lists no Mac (vocaro/queuekeeper#231). A test that leaves a remote call unpatched fails
the same way on every Mac (vocaro/queuekeeper#228).
