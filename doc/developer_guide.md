# Queuekeeper developer guide

How Queuekeeper works inside, and how to change it. Read the [user guide](user_guide.md) first:
this guide assumes you know what a lane, a gate, admission and the farm are.

- [Working on Queuekeeper](#working-on-queuekeeper): the rules for changing this repository
- [Code map](#code-map): where each part lives
- [How it fits together](#how-it-fits-together): a landing end to end, admission and placement,
  plugins
- [The merge queue](#the-merge-queue): rounds, blame and bisecting, pushing, state and recovery
- [The build farm inside](#the-build-farm-inside): how a run reaches another Mac and comes back
- [Admission internals](#admission-internals): slots, queue order, supervision, lock reclamation
- [Scheduling](#scheduling): the goals, the rules in force and the choices behind them
- [Device, simulator and foreground internals](#device-simulator-and-foreground-internals): the
  platform plugins' records and rules
- [Visibility features](#visibility-features): the event journal, and writing a feature
- [Checks this repository runs](#checks-this-repository-runs): spelling and the docs check
- [Maintenance](#maintenance): changing Queuekeeper while work runs, moving a module, renaming a
  setting, the README's screenshots

## Working on Queuekeeper

[AGENTS.md](../AGENTS.md) has the rules every session follows. In short:

- **Edit only in a linked worktree.** `~/Development/queuekeeper` is the shared checkout every
  project's `qk` runs from, so it stays on `main`.
- **Every test and build goes through admission**, even a single test:

  ```sh
  python3 scripts/qk unittest -s tests integration.test_merge_queue.Landing
  python3 scripts/qk run gate
  python3 scripts/qk admit -- COMMAND
  ```

- **Land through the queue**, with `python3 scripts/qk land --wait --json` in the background.
  Never push to `main`.
- **Add no hosted CI.** The queue's gate is this repository's only CI; see [No hosted CI](#no-hosted-ci).
- **Keep the parser and the schema in step.** A setting added to `config.py` goes into
  `schemas/queuekeeper.schema.json` too.

Changing Queuekeeper while other projects' work runs on it has its own rules:
[Changing Queuekeeper while work runs](#changing-queuekeeper-while-work-runs).

## Code map

| Path | What it holds |
|---|---|
| `src/python/queuekeeper/cli.py` | Command dispatch and lane execution |
| `src/python/queuekeeper/merge_queue.py` | Submission, batching, workers and landing |
| `src/python/queuekeeper/agent_cli.py`, `agent_results.py`, `agent_logs.py`, `recovery.py` | Structured results, resumable log reads and recovery advice |
| `src/python/queuekeeper/agent_setup.py`, `agent_hooks.py` | `qk agents`, which wires a project's agents to the queue, and the Claude Code hooks it installs (`qk hook`) |
| `src/python/queuekeeper/worktree_ownership.py` | Records of who made each scratch worktree, and recovery of one its maker left |
| `src/python/queuekeeper/resources.py` | Heavy-work admission on one Mac |
| `src/python/queuekeeper/remote.py`, `offload_host.py` | Placement and running work on another Mac |
| `src/python/queuekeeper/config.py`, `schemas/queuekeeper.schema.json` | Project settings |
| `src/python/queuekeeper/sandbox.py` | A lane's Seatbelt sandbox: its profile, the words that start its command under it, the refusals read from the kernel's log while it runs, and doctor's check that each Mac applies one |
| `src/python/queuekeeper/plugins.py` | Plugin hooks. The core never imports a plugin directly |
| `src/python/queuekeeper/events.py` | The event journal |
| `src/python/queuekeeper/visibility/` | The visibility switch and features |
| `src/python/queuekeeper/docs.py` | The docs check |
| `plugins/` | Separately packaged platform and visibility plugins |
| `queuekeeper-workspace.json` | Which plugins a checkout is composed of |
| `tests/` | Component suites, `integration/`, architecture checks and `test_support/` fixtures |
| `tools/` | Install script, screenshot script and other one-off tools |

Tests set up host isolation first (`import host_isolation`); follow the neighboring tests.

## How it fits together

### A landing, end to end

1. **`qk land`** finds the commits the branch has and `origin/main` lacks, replays them onto
   `origin/main` and every entry ahead in a scratch worktree, and runs the preflight lanes there.
   A conflict or finding refuses the entry with nothing queued.
2. **The entry is queued** under a Git ref of the queue's own, so the branch can move or vanish.
   Queue state is in the repository's Git directory, guarded by a lock, so every worktree shares
   it.
3. **A worker starts** if none runs: one per repository. Each round it fetches `origin/main`,
   takes every waiting entry as one batch, and cherry-picks it into a checkout of its own. It
   first follows Queuekeeper's own `origin/main`, so an old worker never misreads newer settings.
4. **The gate's lanes are placed and admitted** like any other lane (below). Entries arriving
   meanwhile are gated *stacked* on the batch's tip, on another Mac.
5. **On a pass**, the result is recorded, then the exact tested commits are pushed. A
   documentation-only batch skips the code gate and runs the docs lanes instead.
6. **On a failure**, the worker reruns failed tests alone, then looks for one culprit, then
   bisects, gating both halves at once on different Macs.
7. **On no verdict**, it waits for the Mac to settle and runs again; three in a row hold the queue.

Every change of state is appended to [the event journal](#the-event-journal).
[The merge queue](#the-merge-queue), below, has the worker's rules in full.

### Admission and placement

**Admission** is per Mac: one first-come, first-served queue that every lane, `qk admit` command
and gate passes through, in `src/python/queuekeeper/resources.py`. Three slots are guaranteed;
more open one at a time while the CPU is under that Mac's `busyLimit`, memory pressure is normal
and the Mac has settled since the last admission. Over its limit a Mac admits only its first job.
Whole-host and exclusive requests take every slot, and an older request holds later ones back so
nothing starves. Admission never preempts, and an owner that has died holds nothing. Each Mac's
numbers come from its own `admission-limits.json`; `qk admissions limits` prints them. The
[user guide](user_guide.md#admission) covers using it; [Admission
internals](#admission-internals), below, covers its files and mechanics.

**Placement** picks the Mac. Of the Macs with room, judged by each Mac's own admission rules,
the work goes to the one with the most headroom under its own CPU limit, with the farm's order
breaking ties. A waiting whole-host gate takes a place in the queue of every Mac that can run
it, runs on the first to admit it and gives up the others. What a Mac accepts (whole-host gates,
exclusive work, model runs, foreground work, labels) is in its own `mac-settings.json`, never in
a rule naming a Mac.

**Dispatch** pushes the exact commit over SSH into one of four warm checkouts per project on
the target Mac, runs the lane there under that Mac's admission, and journals the run on that Mac
so the sender can reattach after a dropped connection.

### Plugins

A plugin teaches Queuekeeper a platform. A project lists its package in `plugins`, and the
plugin registers commands, lane behavior and checks through the hooks in `plugins.py`. The
core never imports a plugin, and platform behavior stays in its plugin. A plugin that fails to
load stops the run with exit 70 rather than running without it.

`queuekeeper-workspace.json` names each plugin's source folder and the plugins it `requires`; the
architecture tests hold each plugin's imports to that list. `checkout.activate` puts every folder on
the import path of a checkout's `qk` and its children. A process that runs a single module, such as a
consumer's helper, takes `checkout.python_roots(*modules)` instead (`qk python-path` for one not in
Python): the core, each module's plugin and everything that plugin requires, transitively. Keep
`requires` complete when a plugin starts importing another, or the children of such callers lose it.

Each plugin has a README of its own: [Apple](../plugins/apple/src/queuekeeper_apple/README.md),
[Android](../plugins/android/src/queuekeeper_android/README.md),
[Mobile](../plugins/mobile/src/queuekeeper_mobile/README.md), which the Apple and Android plugins
share, [Unity](../plugins/unity/src/queuekeeper_unity/README.md)
and [Mac desktop](../plugins/mac-desktop/src/queuekeeper_mac_desktop/README.md).

The [MCP server](../plugins/mcp/src/queuekeeper_mcp/README.md) is packaged the same way but is no
plugin: a project does not list it, and it imports nothing from Queuekeeper. It runs the
project's launcher with `--json` for each tool, so the commands stay the authority for every
result, and it implements the stdio protocol with the standard library.

## The merge queue

How the worker in `merge_queue.py` runs a round, settles a failure, pushes and recovers. The user
guide's [Land changes](user_guide.md#land-changes) covers what a project and a session see; read it
first.

### A round

The worker fetches, takes everything waiting, oldest first, cherry-picks each entry onto
`origin/main` in its checkout, and runs, through `qk run`:

1. the preflight lanes, `queue.preflight`;
2. the `provision` lane, when the project has one;
3. the docs lanes, when the batch changes documentation beside code;
4. `gate`, or the lanes `queue.lanes` names.

Settings are read from `origin/main` in the worker's own checkout, never from a working tree; the
lanes' own definitions come from the batch. A project without `queue.preflight` has no preflight
lanes, and no lane runs first because of its name. Where the
checkout has no `scripts/qk`, the worker uses Queuekeeper's own `bin/qk`.

- **Short checks skip admission.** The preflight lanes and `qk docs check` run in the worker's
  checkout without admission, so seconds of checking never wait behind a whole-host gate. One that
  passes `DOCS_CHECK_LIMIT` (60 s) runs again admitted. Everything else is admitted and placed as
  any lane is, so `provision` and the gate may run on different Macs; `qk run` runs `provision`
  first on whichever Mac runs a lane.
- **Pushing.** On a pass the worker pushes exactly the commits it tested, each with its own message
  and author, after the pre-push hook (below). A project with `app` then starts
  `qk app keep --after-landing` in the background.
- **Timing.** Each landed entry records the gate's wall time (`seconds`), its own work from
  admission to end (`workSeconds`), its wait for admission (`waitSeconds`), and the CPU busy share of
  the Mac it ran on (`cpuBusy`, read every minute after admission, with that Mac's `busyLimit`).
  The gate's budget judges `workSeconds`, leaving out runs whose busy share reached the limit,
  except whole-host gates.
- **Documentation.** A batch whose every path is unread by `queue.unread` runs the docs lanes and
  no gate. The worker keeps docs-only entries out of code batches: queued beside code, they land
  first in a round of their own; while a code batch gates, a docs-only entry that shares none of
  its paths lands beside it in the `-docs` checkout, polled every `STACK_POLL`. A docs-only entry
  goes ahead of an older entry only when they share no path. One that fails its docs lanes ahead of
  older entries may need one of them, so it records them as `waitsFor` and goes back in line
  behind them.
- **Gate lanes by path.** `lanes_of` runs only the gate lanes `config.affected_lanes` selects: the
  union of the rules matching each changed path, every gate lane when one path is covered by no
  rule nor `queue.unread`, and every one when nothing is selected. The push recomputes this to
  decide whether `queue.hookCoveredBy`'s lanes ran, and records it as the lanes a carry-over by path
  must avoid.
- **Changelog conflicts.** A conflict made only of whole lines both sides added directly below one
  Markdown heading (or list item) of a `CHANGELOG.md` is settled by keeping both, the new lines
  first. The commit is listed under `kept` in the entry's record.

**The checks before queuing** use the worker's own integration code (`Worker.pick`), so a commit
that conflicts there conflicts in a round. The trial is made after every entry ahead, in the order
rounds land them; a `--front` entry is checked against `origin/main` alone. The closing-claim check
reads `landing_proofs.CLAIM`: a keyword negated by `not`, `cannot`, `n't`, `never`, `no longer` or
`without` within three words before it in the same clause. The unittest check runs `qk unittest`'s
pre-run checks on the entry's tree with each `qk unittest` queue lane's `-s` and `-p`. Preflight
runs on the replayed entry, on this Mac, unadmitted, and queues the entry anyway when it cannot
make the check: a lane that is not one plain command, a run past 300 s, an exit other than 0 or 1,
or a finding `origin/main` gives too.

**A branch on a rewritten `main`.** Before any other check, and before a merge refresh is replayed,
`add` calls `rewritten_before_queued` (vocaro/queuekeeper#287). `old_copies` reads the tip's
non-merge commits in `origin/main..TIP` and `origin/main`'s in `TIP..origin/main`. A copy is a tip commit
that matches a commit of `main` on author date, committer date and whole message under another ID.
A history rewrite such as `git filter-repo` keeps all three. A rebase, a cherry-pick or the
queue's own replay gives a new committer date, so a branch that still holds commits it landed
earlier is not counted. Patch identity can't tell those cases apart. `REWRITTEN_COPIES` (3) or more
copies raise `Refused` with a `recovery` of `reasonCode` `rewritten-main`, `action` `rebase`, retryable,
and `git rebase --onto origin/main NEWEST [REF]`. NEWEST is the newest copy in topological order,
so the rebase moves only the commits after it. Non-copies before it, which `main` has in no form,
are listed, up to `REWRITTEN_LISTED`. The two `log` reads are metadata only, with no diffs, and the
second is short for any branch cut from a recent `main`.

**A project's first landing** reads the entry's `queuekeeper.json`, and the batch's in a round,
only while `origin/main` has none that names lanes. The settings that loosen checking
(`hookCoveredBy`, `unread`, `preflight`, `affected`) count only
from `main` even then.

**A `main` whose configuration does not parse.** `add` reads `origin/main`'s file through
`main_config`. When `parse` refuses it, `behind_main_config` first checks whether the tip is behind:
`origin/main` is not its ancestor, `queuekeeper.json` differs between the fork point and
`origin/main`, and the fork point's copy parses. If so, it raises `Refused` with a `recovery` of
`reasonCode` `behind-main`, `action` `rebase`, `retryable` true and `git rebase origin/main`.
`qk land --json` reports that recovery in place of the generic one (vocaro/queuekeeper#279).
Otherwise, when the tip's own file parses, it gates the landing through `as_batch`, as for a first
landing. When it doesn't, the error names `origin/main`'s file as the fault. In a round,
`Worker.settings` does the same: when `judged` holds on `main`'s file, it reads the batch's through
`as_batch`, or raises the original hold when the batch's doesn't read either
(vocaro/queuekeeper#295).

**A repository with no `main`.** `fetch_main` lets the fetch fail with git's "couldn't find remote
ref" (`NO_MAIN`), deletes any stale `origin/main`, and returns False. `add` then reads the tip's
own configuration (`first_config`), records `base` as `None` with every commit to the tip, skips
the trial integration and the preflight lanes, and refuses `--no-gate` and any second entry
(`Refused.waits_for` names the first). The worker's round hands the oldest entry to
`Worker.first_landing`, which makes its checkout at the tip, gates it alone on its lanes for every
path in its tree, and on a pass runs the pre-push hook with the all-zero old value and pushes with
`--force-with-lease=refs/heads/main:`. A lease refused because `main` now exists queues the entry
again, and the next round integrates it as any entry.

### The Queuekeeper a worker runs

Each project's queue has one folder beside the main checkouts, outside every repository:

```
~/Development/queuekeeper-workers/NAME/NAME          the worker's checkout of the project
~/Development/queuekeeper-workers/NAME/NAME-beside   a bisect's newer half, or a stacked gate
~/Development/queuekeeper-workers/NAME/NAME-docs     docs-only entries beside a code gate
~/Development/queuekeeper-workers/NAME/TOOLKIT       its own copy of Queuekeeper
```

`TOOLKIT` is named as Queuekeeper's own checkout is, so a project file that names Queuekeeper by a
relative path finds the copy unchanged. The checkouts are locked against sweeps and keep ignored
inputs a provisioning lane made. Older `../NAME-queue` checkouts move here by themselves.

- **Each round, the copy moves to Queuekeeper's `origin/main`**, and the lanes, the pre-push hook
  and the carry-over checks run with `QUEUEKEEPER_ROOT` naming it. A landed Queuekeeper commit so
  reaches every project's next round; a lagging shared checkout reaches none. A `QUEUEKEEPER_ROOT`
  set where the worker started is left alone.
- **The worker follows its copy.** A worker whose code came from another commit restarts from the
  copy, with the same arguments and bisect, before touching any entry, so an old worker never
  refuses a setting the new code accepts. Queuekeeper's own worker puts the move off for one round
  when it keeps a stacked gate: the landing that made the stack adoptable is the commit it would move
  to, and the new process would throw the stack away. No lane of Queuekeeper's own reads the copy, and
  the stack ran on that landing's tip, so the old worker settles the stack, stacks nothing on it, and
  moves in the next round. A project's lanes do read its copy, so its stack is thrown away first.
- **It never moves to a commit that could leave the queue without a worker.** It first imports the
  worker's code from that commit in a separate process, and records the commit as `moved` until a
  worker started there finishes a round. A commit that does not import, or on which a moved worker
  died or held first, is not moved to, and the log says `staying on the toolkit at COMMIT`. A
  broken commit pushed by hand is then fixed through Queuekeeper's own queue like any other.
- **A worker whose session ended exits.** macOS denies such a worker the local network. While
  another Mac is configured, the worker checks before each round, and a lane's `qk run` exiting 69
  finds it too; either way the worker exits holding nothing, and the next `qk land`,
  `qk queue wait` or `qk queue resume` starts another.

**The shared checkout follows each Queuekeeper landing.** After each push, Queuekeeper's own worker
fast-forwards `~/Development/queuekeeper` to `origin/main`, without hooks, only when that checkout
is on `main`, clean, behind, holds no commit of its own (a commit made there and since landed is
found by `git cherry` and passed with `git reset --keep`), and no process started from it still
runs. Sessions under `.claude/worktrees/`, Git's own directory, the signed launcher, and processes
registered in `queuekeeper-restartable/PID.json` (such as `qk serve`, which execs the new code when
HEAD moves) do not count. A checkout left only for running processes is retried every 30 seconds
while nothing waits, and `qk queue status` and `qk queue doctor` name up to five holders. A checkout
left for any other reason is a person's to settle; a session never stashes, resets or edits it to
make it follow. Nothing here holds the queue, and none of it keeps the queue from reading idle: while
`Worker.catch_up` retries, the round's account marks `catchingUp`, and `reading` counts a live worker
so marked as idle when no entry waits, since nothing is left to test or push and the holder can be
another project's worker for as long as its gates take (vocaro/queuekeeper#285).

### Gates beside each other

**A bisect's two halves gate at once.** The older half is offered as any gate is; once a Mac has
admitted its last lane, the newer half runs in the `-beside` checkout on any other Mac that can
start it now (`qk run --offload-only` when the older half is on this Mac, otherwise
`qk run --if-free` with that Mac left out). Whichever half passes lands. When both fail, the older
is bisected first.

**When both halves pass,** no entry fails alone: the entries fail together. A half counts as
passed when its failed tests all pass alone on rerun, as a batch with flakes does; the newer half's
failed tests are rerun before either half lands, so a flake in one half does not hide the
interaction. `Worker.together` gates the whole set again in the `-beside` checkout, since `main`
may have moved, then subsets of it, keeping each subset's verdict (`Joint`). `fewest` drops runs of
entries while the rest still fail, halves first (whose verdicts the bisect already has) and single
entries last, so every entry left is needed; each one left is then gated alone, two at a time, one
in the worker's checkout and one in `-beside` on another Mac, as the halves are. One that fails
alone comes back alone. Otherwise each comes back with `conflictWith`, the other entries' IDs,
commits and `state` (`failed`), and the rest gate again. A subset that fails counts only once a
rerun of its failed tests alone fails again (`Probe.confirm`); one whose failed tests all pass alone
counts as no proof, as does one that does not apply to `main` by itself, so their entries stay. A
probe with no verdict ends the search as any such gate ends a round, and the next round gates the
halves again. The search's gates and wall time go in the round's account (`together`) and a
`round.together` event. A half that passed by flakes does not land when the search runs, so its
flakes are recorded only once the round knows whether it does (`Worker.hold_flakes`,
`Worker.release`): their `outcome` then says what the search did, and `test.flaky` has `landed`
false (vocaro/queuekeeper#302).

**When a bisect gates one half at a time,** a passing older half lands at once, so a newer entry
that breaks only with it fails alone on a `main` that holds it. The worker remembers what the bisect
landed and the `main` before it. When it gates an entry alone with such landings under it, it also
gates the entry on that earlier `main`, in the `-beside` checkout on another Mac once a Mac has
admitted the entry's own final lane (`Beforehand`), as a bisect's halves gate. A pass of the entry
cancels that probe, whose lane `Worker.gate` records as `canceled`, as it does a thrown-away
stacked gate's: in the entry's `logs` and the `gate.ended` event, not as unmeasured. A failure
waits for it (`Worker.partners_landed`); if the entry passes on the earlier `main`, `fewest` finds the landed entries it fails with, and it comes back naming them in
`conflictWith` with `state` `landed`, to rebase onto `origin/main`. With another Mac free the check
costs Mac time, not wall time; when no other Mac can start it, or a lane runs only on this Mac, the
probe gates after the entry fails, and several landed entries cost more gates to search. The search follows mergetrain's `minimize_joint_failure` (MIT; see `THIRD_PARTY_NOTICES.md`). The newer half waits behind the older instead when no other Mac can start it, a
lane must run on this Mac, the round is docs-only, ungated or `--front`, or a newer entry applies
only on top of an older one.

**Stacked gates.** While a batch gates, the worker looks every `STACK_POLL` (30 s) for entries
behind it, and once the batch's last lane is admitted, gates them stacked on the batch's tip in the
`-beside` checkout, on another Mac, where it waits for admission as any gate does: with the batch
on this Mac, `qk run --offload-wait` sends each lane to another Mac to wait in its queue. The batch's push makes the stack's base `origin/main`, so the
next round adopts it. Anything else throws it away and requeues its entries: the batch fails, gives
no verdict or lands at another commit; the stack gives no verdict; an entry is removed or put
first; `main`'s lanes change; the queue holds; or the worker stops or moves (except the move
Queuekeeper's own worker puts off, above). Before it integrates
a stack, the worker reads the capacity of each Mac the stack may go to, every Mac but the batch's,
and judges it for the stack's final lane by the test placement uses before it offers a lane
(`remote.spare`): a free slot, the CPU under the Mac's heavy busy limit while admitted work runs
there, a settled Mac and normal memory pressure. When none has room, it starts nothing, says so
once, and waits two minutes before looking again: most stacks used to integrate, mark their entries
testing, and wait only to be declined. A check of the CPU alone still let stacks through to a Mac
already running other work, which admission then declined. A reading can still be a minute old when
the final lane asks. With one try at another Mac and no wait (`--offload-only`), a stack kept off
this Mac was declined then, so it waits there instead.
When no Mac admits a stack's next lane, the worker waits two minutes before trying another. A stack settles nothing: an
entry that conflicts with the batch below waits for its own round. A bisect never stacks.

**Removal.** The worker looks every `REMOVAL_POLL` (5 s) for an entry that has left a running gate,
stops that gate's lanes on whichever Mac, freeing admission, and gates the rest with no verdict
counted. The same poll lets `--front --no-gate` overtake a gate still waiting for admission, an
unlocked screen or a Mac.

**A lane that changes its tree.** After each lane that passes, `Worker.unaltered` compares the
checkout with the commit the gate tests (`altered`: `git diff --name-only` against it, and `HEAD`).
For a lane run on another Mac, `offload_host.altered` makes the same comparison there, and the
dispatch receipt carries the result back as `result.altered`. Any tracked change fails the lane
with `ALTERED`. `weigh_failure` skips reruns, and `rejection_facts` names the changed files. The
push sends the commit the worker built, so without this check a lane that rewrote sources would
land a tree it never tested. Before `older` reruns or blames, `Worker.main_alters` resets the
checkout to the base, gates that one lane there, and restores the failed gate's fields and head.
When the lane changes the base's tree too, the change is `main`'s, so the batch is queued again
and the queue holds for a session instead of bisecting every entry back (vocaro/queuekeeper#264).

### Flakes, blame and bisecting

These apply to a failing `qk unittest` lane, or a group whose only failure is one, and to a lane
with `failedTests`. A plugin can stand in for a lane's `results` and `rerun`: a reader in
`plugins.RERUNS` gives a `plugins.Reruns` for a lane whose command it knows, with the argv that
reruns one test and a function that names the failed tests from the lane's results folder.
`merge_queue.declared_failures` takes it when the lane's `failedTests` has no `results`, and
`config.failed_tests_setting` then takes `paths` alone. The Unity plugin gives one for a
`qk unity test` lane (vocaro/queuekeeper#109).

- **Rerun alone.** The worker reads the failed test IDs from unittest's `FAIL:` and `ERROR:` lines,
  waits for the Mac to settle, and reruns each alone with the lane's `-s`, `-p`, `-t`, `--split`,
  `--batch-size` and `--timeout`, without `--fail-fast`, up to `RERUNS` (3) times. Which options a
  rerun keeps comes from `qk unittest`'s own parser, `parallel.unittest_parser`, by each option's
  role in `parallel.UNITTEST_ROLES`, which `qk doctor`'s lane coverage reads too; a new option needs
  a role there, and `core.test_parallel.Options` fails until it has one. Any failing or
  unmeasured rerun makes the failure real. While reruns wait and run, the worker writes
  `.build/queuekeeper/.waits/rerun.json` in its checkout, which `qk queue status` reads for each
  rerun's test, count, log and admission wait; the record goes once the verdict is in.
- **A gate cut short.** A `--fail-fast` gate whose failed tests all pass alone does not land, since
  the rest of its suite never ran: it gates again, once for that commit, and bisects if it stops
  that way again. So does a `failFast` group that its first failed member ended: `parallel.lanes`
  cancels the other members' runs and gives each a receipt whose outcome is `parallel.CUT_SHORT`,
  which `Worker.read_group` puts in `group_cut` rather than among the failed members, so that the
  failed member's tests are still rerun and blamed.
- **A flake lands only if untouched.** When every rerun passes, the batch lands only if no commit
  changes the test's module or a project file it imports, followed through the module's own import
  statements at any depth. The flake is recorded in the entry's `flakes`, the worker log, a
  `Flaky-test:` trailer, and the tracker (a comment on the open issue naming the test, or a new
  issue), quoting at most `FAILURE_EXCERPT_LIMIT` (4,000) characters of the failure. A tracker
  `gh` cannot reach stops nothing.
- **A test the batch adds or one entry changes** is never a flake. A test the batch adds has its
  class and method defined in the batch and absent on `origin/main`. A test one entry changes is
  on `origin/main`, and exactly one entry's commits change its method's own definition, compared
  as a syntax tree from before the entry's first commit to its tip (`Worker.changer`), so a
  comment is no change. Changing only the test's module elsewhere is not enough; a test that no
  entry or several change is judged as any flake. When such tests alone explain the failure, the
  one entry that adds or changes them is sent back and the rest gate again at once; any doubt
  bisects (vocaro/queuekeeper#129).
- **One culprit.** A failed test points at an entry when that entry changes a path the rerun's
  output names, the test's module, or a file it imports. When every failed test whose rerun failed
  points at one entry, the worker picks the other entries onto the base without it and reruns each
  such test once; only if each passes is that entry sent back. The confirmation exists because a
  test can read a file neither its imports nor its output name. Failed tests that passed alone
  beside the real failures do not stop the blame: once it stands, they are filed as flakes, and the
  rest of the batch gates again at once (vocaro/queuekeeper#88). One that another entry adds or
  changes bisects instead. Any doubt bisects: real failures that point at several entries or none,
  or a rerun with no verdict.
- **A refused gate.** A `qk unittest` lane whose log has no summary and a refusal ending
  `nothing ran` is not rerun. The worker sends back the one entry whose tree alone gives the
  refusal or that changes a module it names; with none or several, it bisects. The same refusal in
  the next gate, or in both halves, with nobody to blame, holds the queue for a session.
- **Bisect at once** when the lane is not `qk unittest` and has no `failedTests`, its log names no
  test to rerun, more than `RERUN_MOST` (5) tests failed, or the Mac does not settle within
  `SETTLE_LIMIT`. A `qk unittest` lane with an option a rerun cannot carry bisects too, and the
  worker's log names the option.

A send-back's reason ends with the Mac's load, split into readings while the lane ran and while it
waited, and for each failed test whether the entry's commits change its file, saying when a test's
file could not be placed. For a preflight or docs lane, the worker reruns it on `origin/main` alone
and says whether `main` fails it too.

### Runs with no verdict

Exits 3 (unmeasured), 69 (a worker without Local Network access), 75 (no admission) and 78 (a
misconfigured Mac) blame no commit. The worker runs the lanes again, and holds once
`queue.noVerdictRuns` (3) runs in a row have given no verdict.

- **Tests that all passed.** A failed `qk unittest` gate whose last summary says `OK` names no
  test to rerun or bisect for (`Worker.weigh_passed`). When it ran too few tests (the count
  history's `fewer than`, or `minimumTests`), the one entry whose commits remove more `def test`
  definitions from the suite than they add goes back with the count, and the rest gate again at
  once; with none or several, the queue holds at once for a session. Bisecting cannot find such an
  entry, since each half may run on a Mac with no count to compare with. Any other failure after
  passing tests, such as a step after them, is no verdict and counts toward `queue.noVerdictRuns`
  (vocaro/queuekeeper#290).
- **Settling.** Each retry first waits, polling every `SETTLE_POLL` (15 s) for up to `SETTLE_LIMIT`
  (an hour), for the Mac it could run on to read a CPU busy share under its `busyLimit` and
  normal memory pressure (`resources.pressure`). With another Mac configured it reads the Macs the
  retry could use (`remote.retry_macs`); another Mac is asked over SSH.
- **Locked screens.** An exit 3, or a lane at its limit, whose last screen reading (receipt's
  `screenAfter`, else the log's last `screen:` line) shows a lock does not count; the lane runs next
  as `qk run --unlocked-screen LANE`. `LOCKED_SCREEN_RUNS` (5) runs in a row that started unlocked
  and ended locked hold the queue.
- **Could not start.** A lane's receipt with `outcome` `not-started` (exit 127) holds at once; the
  worker reads the receipt, since a lane's own command may exit 127. Exit 70 with no receipt of the
  lane means Queuekeeper could not start: the worker starts its own checkout with `--version`, with
  and without the batch, within `START_LIMIT` (60 s). If only the batch breaks it, the batch is
  bisected; otherwise the queue holds for a session.
- **Disk space.** A lane that ends with `queuekeeper: out of disk space:` holds at once.
- **Sleep.** A lane that fails while its Mac sleeps over 5 s during its command, as a lid closed on
  battery does, ends unmeasured (exit 3), and its receipt's error and `sleptSeconds` say how long:
  a test's time limit runs on through the sleep while nothing runs. A pass through a sleep keeps its
  pass, and a failed `compileFirst` build stays failed (`cli.slept_since`).
- **What the hold says.** How many runs, on which Mac, and why: the lane at its limit, the first
  unmeasured test and last screen reading, the receipt's error, no admission, or the Mac's
  configuration, else the project's last `Unmeasured:` line (cut at `UNMEASURED_SHOWN`, 300
  characters). The hold is the owner's only when the last exit was 78.

A lane may take `LANE_LIMIT` (6 hours: the default 2-hour lane limit and two more for its wait).

### Reaching the remote and pushing

**An unreachable remote** (a name that did not resolve, a connection refused, timed out, reset or
dropped) is retried after `BACKOFF` (10, 30, 90, 270 s); after the fifth failure the round runs
again after `CLEARING_WAITS` (1, 5, 15 minutes), keeping every entry and any pass, and holds for a
session only when the last of those fails too, about an hour in. Git's message decides: a remote
that answered and refused (authentication, a 4xx status, a refused key, a missing repository, a
rejected push) holds at once for the owner, and any other error holds at once for a session.

**A push refused for its credentials** while the fetch just before it succeeded from the same URL is
retried through `BACKOFF`. On GitHub the worker then reads githubstatus.com and, while Git Operations
is not operational, retries every minute for up to `SETTLE_LIMIT`. Otherwise it holds, giving
GitHub's status.

**The pre-push hook runs ahead of the push.** `git push` connects before it runs the hook, so a long
hook held an SSH connection until GitHub dropped it. The worker instead runs the hook with
`git hook run`, logging to `queuekeeper-queue/logs`, then pushes with `--no-verify`. A push that
races or drops is retried without rerunning the hook; a carried-over commit is new and runs it
again. `queue.hookCoveredBy` skips the hook, and a docs-only batch always does.

**Main moving under a passed batch.** If `main` moved only by unread paths the batch does not touch,
the worker checks that the batch's patch is unchanged on the new `main`, runs `qk docs check` when a
document moved and then `queue.carryOverChecks` (supervised, with `LANE_LIMIT`), and pushes. Any
other move, or a failing check, gates again with no blame. A moved path that `queue.affected` covers
and whose lanes are none the batch's own paths selected (`carried_by_rules`) carries over too, after
`qk preflight` passes on the rebased tree; a batch that ran a `provision` lane, a docs-only batch
and an ungated landing carry over this way never.

### State and recovery

State lives under `queuekeeper-queue` in the repository's common Git directory:

| File | What it holds |
|---|---|
| the entries and the lock | Every entry, its ref and its state |
| `round.json` | The worker's account of its round: bisect suspects, each gate's lane, Mac, age, what it waits for and since when, any stack |
| `closed.json` | A closure: why, who, when, `until` (its deadline) and the closer's `worktree` |
| `worker.log` | The worker's log |
| `logs/ROUND-LANE.log` | Each gate's log, which `qk queue status` names |
| `scratch/ID.json` | The record of each scratch worktree `qk land` has made and not yet removed |

What a gate waits for comes from `.build/queuekeeper/.waits/LANE.json`, which `qk run` keeps on
any Mac (another Mac sends it back as it changes). It says only what a lane waits for, never how it
ended; a stale one is ignored. A gate held up for `STUCK_SECONDS` (15 minutes) by a dead admission
entry shows as `stuck:`, pointing at `qk admissions recover`. A gate on another Mac keeps its full
output in the checkout's `.build/queuekeeper/remote/.dispatches/LANE/*/run.log`, copied into the
gate's log when the lane ends.

- A worker starts with `qk land`, `qk queue wait` or `qk queue resume`, and exits when nothing
  waits. After taking the lock, it requests a stop for each run whose dispatcher is proven gone.
- After that, it removes each scratch worktree whose maker its record proves gone
  (`worktree_ownership.reclaim`, as `qk queue reclaim` runs it). A scratch worktree's lock names its
  record and the record its path, so neither a path nor a lock's words alone is authority to delete.
  A maker counts as gone only when its PID is gone or runs another start; one that cannot be read
  counts as running. The record is written before `git worktree add` and removed only once the
  worktree is no longer registered, so a kill at any point leaves a record for anything it left.
- `qk land --wait` and `qk queue reclaim` delete a landed entry's local branch only by
  `landed_cleanup`'s rule: the queue's newest entry from it landed and `origin/main` holds that
  landing, its tip is the one queued or every commit of it has an equal patch on `origin/main`,
  and no worktree holds it. They report worktrees that hold only landed work and never remove one.
  A worker runs the branch half at its start too (`reclaim_branches`, after `reclaim_scratch`),
  saying only what it deleted. The worktree report needs an `lsof` census, so it stays in
  `qk queue reclaim`. `qk land --wait --detach-landed` passes `detach_in`, the top of the worktree
  `qk land` ran in: a branch kept only because that worktree has it checked out goes through
  `landed_cleanup.detach`. It runs the same rule as though no worktree held the branch, requires
  that worktree's HEAD to name the branch at its tip with no tracked change and no operation under
  way, runs `git switch --detach TIP` (same commit, hooks off), then `git update-ref -d`. If the
  deletion is refused, it writes HEAD back to the branch. No other worktree is ever changed
  (vocaro/queuekeeper#311).
- What a dead worker was testing was never pushed, so the next worker tests it again.
- A batch whose lanes passed is recorded `passed`, with its commit and base, before its push. After
  a hold and `qk queue resume`, the next worker pushes it without gating again while the batch is
  unchanged.
- Before each `git push`, `Worker.push` records the attempt on every entry of the batch, in a
  `publishing` field that lists each pushed head with the base it went on. A push whose answer
  never comes leaves that field behind: a timeout (`GIT_TIMEOUT`, as a hold that clears by
  itself), a connection dropped past `BACKOFF`, or the worker's death. Such an entry is
  `publication-unverified`. The field goes away when the entry settles, when the remote answers
  with a refusal, or when a carry gives up.
- `Worker.reconcile` runs at the start of every round, before anything is pushed or gated. It
  fetches `origin/main` and asks `git merge-base --is-ancestor` whether main holds any recorded
  head. If it does, the entries land as that push would have landed them (`Worker.settle_pushed`, with
  `batch.pushed` marked `reconciled`), with no push, pre-push hook or gate. This works even when
  main has moved on since. Otherwise the push moves to `pushedBefore` and the round goes on as before. The
  fetch at the top of each try in `Worker.push` asks the same question, so a push whose connection
  dropped after it landed is recognised there, even once main has moved past it.
- Each attempt in `publishing` also records the PID of its `git push` and that process's start, as
  the kernel gives it. A worker killed alone, such as by SIGKILL to its Python process, leaves its
  push running, and a push that landed after `reconcile`'s fetch said it had not would be pushed or
  gated again over commits main then holds (vocaro/queuekeeper#265). So before its fetch,
  `reconcile` waits for any recorded push still running as the same process (`Worker.outlive`).
  A push still running `GIT_TIMEOUT` after it started, as long as its own worker would have given
  it, is ended, and the fetch waits `PUSH_SETTLE` more for a remote that had its whole pack.
  A PID that now runs another process is no push, and is neither waited for nor signalled.
- An ended push can still land once the remote applies what it had taken, after the fetch said
  it had not. So a push the fetch does not find is kept on its entries, in `pushedBefore`, until
  they settle. Each round's `reconcile` and the fetch at the top of each push try ask about it
  again (`Worker.settle_kept`). A kept push found there lands its entries as that push would
  have, and the rest of the batch is gated again.
- Without this, a landed push was recognised only while `origin/main` was still its commit. Once
  main moved on, the batch was carried over or gated again, cherry-picking commits main already
  held, or sent back as a conflict with itself. A worker that died while settling a batch left
  entries that `push_passed` no longer took for the batch that passed. `push_passed` also resets
  the checkout to the commit that passed: a worker that died mid-carry left part of the batch
  there.
- The push stays a plain fast-forward push, without `--atomic` or `--force-with-lease`. It updates
  one ref, so `--atomic` adds nothing. A fast-forward-only push already refuses any `main` that is
  not an ancestor of the pushed commit. A lease would add only a refusal of a `main` that moved
  forward to an ancestor of it, which the push lands correctly anyway. Its "stale info" refusal
  would also arrive as a hold rather than the carry a rejection gets.
- The closure is read by `qk land` and `qk queue add` twice, before the conflict check and under the
  lock that saves an entry, which `qk queue close` takes too, so an entry is either saved before
  the close returns or refused. The worker does not read it.
- A closure lapses at its `until`, which `close` sets an hour ahead, or `--for` ahead, at most
  24 hours (`CLOSE_SECONDS`, `CLOSE_LIMIT_SECONDS`; vocaro/queuekeeper#286). `Queue.closed()`
  reads a closure past its deadline as open, with no side effects, so nothing depends on a lapse
  being tidied. `Queue.lapse()` removes such a file under the entries' lock and journals
  `queue.lapsed` once. `qk land`, `qk queue add`, the snapshot and `qk queue closed` call it.
  A record without `until`, written before deadlines existed, lapses an hour after its `at`. A
  record that cannot be read lapses an hour after its file's modification time. Either way it
  still closes the queue until then. `close --extend` (`Queue.extend`) rewrites `until` under the
  same lock. It renews only a closure that has not lapsed.
- The closer that matters is the agent session that ran `close`, not the command itself, which
  exits at once. No process can be checked for whether that session is alive, so the deadline is
  the backstop. `qk queue doctor` adds a hint by checking whether the closer's recorded worktree
  still exists.
- A project with `"integration": "pull-request"`, or with no gate lane, is refused.

**`qk queue doctor`** reads the entries, the hold, whether a worker runs, and the logs of up to the
twelve newest `qk unittest` gates back to the last verdict. It reports refused gates (and the one
entry whose tree gives the refusal), waiting entries `qk unittest` would refuse, two or more gates
in a row with no verdict, an entry that came back holding the same patch as an earlier failure,
entries with no worker, a hold, and a gate waiting `IDLE_WAIT_SECONDS` (15 minutes) for admission on
a Mac under its busy limit, naming every holder there with its command, PID, age, CPU and session.
What cannot be read is `unknown`, never guessed light. It exits 0 when healthy and 1 otherwise, and
changes nothing; restarting a dead worker is not its job yet.

### What a receipt records

The user guide's [Receipts and outcomes](user_guide.md#receipts-and-outcomes) and
[Test counts](user_guide.md#test-counts) cover what a project sees. Underneath:

- **Where.** A run that starts while another run of the same lane from the same checkout runs on
  this Mac writes its log and receipt to a folder of its own, which its summary names. The receipt
  records the project's and Queuekeeper's commits with a SHA-256 of their working changes, the
  policy commit for a heavy or exclusive lane, and the argv with its `arguments` apart.
- **Counting.** An XCTest log counts each bundle's top-level `All tests` (or `Selected tests`)
  total, since `swift test` and `xcodebuild` run each bundle under its own top suite. A wrapper's
  own `Executed N tests` line counts only when the log holds no XCTest totals it would repeat.
  Inside a `qk unittest` process's output, only unittest's own total counts, since Queuekeeper's
  tests print lines that imitate other runners. Swift Testing's per-test skip lines are subtracted. Swift Testing prints a canceled case of a
  parameterized test as passed, so `qk xctest` adds a skip line for each test with one, from the
  process's event stream (`xctest_shards.canceled_cases`, vocaro/queuekeeper#312).
  A run short of its floor because a test was left unmeasured is unmeasured, not failed.
- **Lane groups.** A group writes no `LANE/source.json` for its lanes, and its own receipt has no
  `tests`. Each lane's `executedTests` and `skippedTests` (`null` when its log reports none) are
  under `lanes.NAME` in the group's `source.json` and in `lanes/NAME.json` beside `lanes/NAME.log`.
  A group run on another Mac brings its `lanes/` back under `.build/queuekeeper/remote/GROUP/`.
- **History.** `count_history.py` searches up to 200 first-parent commits of `main` (`origin/main`,
  or local `main` without an origin) below the run's commit for the newest passing whole run on the
  same Mac that recorded a count. A Mac with none is held to the newest such run on any Mac that
  recorded its executed and skipped counts: the tests that run selected (executed plus skipped),
  less those this run skipped, so a test that skips on one Mac and not the other is no fall, and
  the message names that Mac and commit. Only runs on `main` set the count, so a branch cannot
  lower it. A `Tests-Removed:` trailer counts only in a commit after the run compared with.
  `exactTests` lanes are held to their own count instead.
- **Outcome details.** A failure before any test (exit 4, `lane_report.before_tests`) quotes the
  log's last error under `beforeTests`. A run that failed or was unmeasured without counting or
  naming a test quotes its last five non-blank lines of its own (`lane_report.own_output`). A
  timeout whose cleanup cannot confirm every process ended keeps its admission for recovery but
  still exits 3.
- **Load.** `load` keeps the Mac's one-minute load, CPU busy share and busy limit every 60 seconds
  and at the end (`lane_report.RunLoad`), read where the lane ran. Both figures include the lane's
  own work. `cli.record` starts it once the lane is admitted, so the wait for admission is no part
  of it. `lane_report.load_said` words it for `qk run`'s summary and for the closing `remote:` line
  of `qk remote run` (`remote.execute`'s `says_load`), which has no summary of its own.
- **Scroll bars.** Each Mac run records the host's scroll-bar kind, read and never set: the global
  `AppleShowScrollBars` (or `unset`) and any external pointing device, under `scrollBars`, with one
  `queuekeeper: scroll bars:` log line per Mac. An unreadable value is `unknown`, never `unset`.
- **Stall observation.** With `stall`, the supervisor samples the lane's log metadata and its owned
  processes' cumulative CPU once a second, checking each PID's start identity and leaving out nested
  admission owners. A changed process set or log counts as progress; an unreadable sample or a gap
  over three sampling intervals (such as sleep) resets the quiet interval. Each leaf writes
  `results/progress.json` (with `observedSeconds` and `maximumObservationGapSeconds`, to review
  coverage before choosing a bound) and publishes a live copy in its wait record's namespace for
  `qk queue status` and `qk admissions now`; a copy expires 15 seconds after its last observation.
  Stopping rechecks output and CPU, uses the supervisor's TERM and KILL, and reports `Stalled` as
  unmeasured only after the owned tree has ended.

## The build farm inside

How work reaches another Mac and comes back. The [user guide](user_guide.md#the-build-farm)
covers placement and setup as a user sees them; this is the mechanism, in `remote.py`,
`offload_host.py` and `farm.py`. `qk run`, `qk admit`, `qk remote run` and `qk remote admit` all
send work the same way.

### Reading a Mac's capacity

Every Mac, this one included, is judged by one test: what its own admission would admit now. The
reading is `offload_host.py`'s `capacity` answer, run in the Mac's login shell: guaranteed slots
and their maximum, busy slots, `cpuBusy` (sampled over up to two seconds), `busyLimit`, the
whole-host and exclusive work running, its screen, and its `takes`, `labels`, `settingsFile` and
`settingsIgnored`.
A Mac has spare capacity when:

- a guaranteed slot is free (every one for a whole-host lane, every one up to `HEAVY_SLOTS_MAX`
  for exclusive work), or, for ordinary work, admission would open another: the Mac has settled
  for `HEAVY_SETTLE_SECONDS` and memory pressure is normal;
- its CPU is under its `HEAVY_BUSY_LIMIT`, or nothing admitted runs there;
- no whole-host or exclusive work runs there, no whole-host request queued within
  `HEAVY_PLACE_SECONDS` keeps the guaranteed slots, and no queued exclusive request holds later
  work back.

Queued requests count as admission counts them: one whose process has ended, or whose owner
can't be confirmed running, keeps nothing. A lane's own place in this Mac's queue is not counted
against it. A reading from an older toolkit lacks some fields and withholds nothing by them.
`qk queue placement`, `qk admissions usage` and the merge queue's wait before a retry apply the
same test.

A reading spans about 15 seconds and a gate's load dips between phases, so several placements can
pick the same quiet Mac. That, and work leaving a warm checkout for a little more headroom, are
accepted costs of placing by headroom.

### Offers and waits

1. **Another Mac** runs the lane when its reading shows room, no Mac with room has more headroom,
   and it can start the lane now: `qk run --if-free` there admits it within five seconds and one
   of the project's four checkouts is free.
2. **This Mac** takes the lane at once, offering nothing, when it has the most headroom. A Mac
   whose reading showed room may still decline once offered (every checkout in use, the model
   held); this Mac's admission then gets the same five seconds.
3. **No Mac with room**: the lane waits in this Mac's queue, 15 s at first, doubling to 120 s, and
   between waits the Macs are read and offered again. A repeated offer sends no commits again.
   After 5,400 s unadmitted the lane exits 75.

A request keeps its arrival across retries and its place in this Mac's queue across waits. A Mac
missing one offer, the first included, is asked again within 30 s while the lane waits in this
Mac's queue; only a second miss in a row counts it unreachable,
since one slow `ssh true` would otherwise keep a reachable Mac out of rotation for minutes. One Mac of
several missed while the others were busy is asked again within 30 s once, and after that with the
others; `qk queue status` names it. Exclusive
work parks for up to `EXCLUSIVE_PARK_SECONDS` (900), offered with `--if-free` on the same
backoff, before it queues on a Mac at its first arrival.

### The commit sent

- **The project.** HEAD when the working tree matches it; otherwise a snapshot commit built
  through a copy of the index (`GIT_INDEX_FILE`), `git write-tree` and `git commit-tree`, HEAD its
  parent. The session's index, HEAD and branch are untouched and no ref holds the snapshot. The
  index copy keeps the original's modification time, or Git trusts stale stats for a file
  rewritten at the same size in the same second.
- **The toolkit** goes the same way, as a snapshot when it has changes. A toolkit checkout that is
  clean, on `main` and only behind its fetched `origin/main` sends `origin/main`'s commit instead,
  since the shared checkout lags while processes run from it. A queue's gate uses a clean copy of
  the toolkit.
- **Snapshots** keep their receipts in `.build/queuekeeper/remote/LANE/` but are never filed by
  commit, so `qk remote passed` ignores them.

### Checkouts and pushes

Commits are pushed over SSH into bare repositories under `~/Development/queuekeeper-farm` on the
other Mac, never through GitHub. `offload_host.py` then takes one of four persistent checkouts,
checks the commit out detached with the toolkit beside it, and runs `qk run` or the command there.

- **Warm by nearness.** Of the free checkouts, a run takes the one whose last tree differs from
  its commit in the fewest files, and the log says which. Index order alone once doubled a gate's
  first step on a cold checkout.
- **Changed checkouts** are passed over and kept; a wait or decline names their changed files.
- **A named slot** (`--checkout-slot N`) waits under that slot's lock with no place in line.
  A checkout slot is neither a heavy-work slot nor the foreground reservation, so waiting for one
  holds neither.
- **Provision first.** The project's `provision` lane runs as `qk run --here provision`, admitted,
  with `--if-free` when the work was offered to start now.
- **No bytecode.** The work runs with `PYTHONDONTWRITEBYTECODE=1`.
- **`QUEUEKEEPER_DISPATCHED_FROM`** is set to the sender's name. Code tests only that it is set.

**Warm pushes.** A push learns what the receiver has only from advertised refs, so each repository
there names every commit its checkouts hold as `refs/queuekeeper/checkouts/COMMIT`, renamed before
each push. Without them a one-line change sent the whole history. No Git setting is used:
`push.negotiate` needs protocol v2, which a stock macOS sshd does not pass.

**Races.** Two dispatches pushing one commit race for its ref, and the loser gets
`cannot lock ref ...: reference already exists`. Queuekeeper then checks with `git ls-remote`
whether either name holds the commit and goes on if so; otherwise it pushes again, three attempts
in all. Any other failure is not retried.

**Bounds.** A push not finished in 30 minutes, or stopped, is ended with its ssh, and nothing of it
enters the repository. The receipt's `preparation` records each push's phases, objects, bytes,
warm or cold, and each Mac's `git --version`.

### The size of the ssh command

`offload_host.py` travels compressed inside the ssh command, since it runs before the other Mac
has the toolkit, and OpenSSH bounds a packet at 256 KiB. A plan, and a reconnection, carry only
the functions they run, with blank lines where the rest stood so tracebacks keep line numbers
(`remote.plan_program`); a question such as capacity or screen carries the whole file. Tests hold
a plan's command under two thirds of 128 KiB and a question's under 192 KiB. A part that needs
nothing of the file, such as the holder of a waiting gate's place, is a file of its own
(`place_holder.py`).

### What comes back

- **Output** streams back as written, into `.build/queuekeeper/remote/LANE/`. Each dispatch also
  has its own copy in `.build/queuekeeper/remote/.dispatches/LANE/TIME-PID/` (newest eight kept);
  `LANE/` holds whichever ended last, so summaries and plugin results are always read from the
  run's own dispatch.
- **Exit status** is the work's, or 1 for a run stopped before the work began.
- **Passes by commit.** A whole lane's receipt is kept by commit in the repository's Git
  directory, with its `results/` beside it, so `qk remote passed` answers from any worktree. A
  whole lane run here at a clean commit by a clean toolkit is kept too, with `ranOn` `this Mac`.
- **The receipt** records `commit` and `head` (different only for a snapshot), the toolkit's
  commits, `ranOn`, `mac`, and for a farm run `host`, `farmMac` and `macId` (the hardware UUID).
  The other Mac's own receipt is nested inside, naming the sender in `dispatchedFrom`. Older
  receipts say `build host` or `second Mac` in `ranOn` and still count.
- **A command's edits** come back as `changes.patch`, applied with `git apply --binary` to the
  working tree only while it is still the tree sent; otherwise kept as
  `.build/queuekeeper/remote/unapplied/DISPATCH.patch` with exit 73. A lane brings back no patch.
- `qk remote run --serial GROUP` runs a group's lanes one at a time there as a serial reference;
  its receipt is no pass of the group.

### Running here after an offer

A lane this Mac takes runs in this checkout only while its tree is still the commit's tree.

- **The project changed meanwhile**: it runs the commit sent in a temporary worktree, with the
  toolkit beside it and `QUEUEKEEPER_ROOT` naming it, `provision` first. The worktree is removed
  when the lane ends; only `QUEUEKEEPER_RESULTS_DIR` is kept. The receipt's `sent` and `sentTree`
  say what ran. If no worktree can be made, the lane exits 2.
- **Only the toolkit changed**: it runs in this checkout with a temporary toolkit worktree,
  recorded as `sentToolkit`.
- **`qk admit`** does the same; its edits come back as from another Mac, usually as a kept patch.

A lane that runs here for any reason runs `qk run --here provision` first, inside the lane's
admission; a failing provision exits 78, no verdict. Nested work, the provision lane itself and
work another Mac sent run none. A second run of a lane from the same checkout keeps its files in
`.build/queuekeeper/.runs/LANE/TIME-PID/`. Every `qk run` ends with the summary from
`lane_report.py`; a run on another Mac leaves it to the sender.

### Stops, sleep and lost connections

- **A stop carries over**: the other Mac stops the run and it leaves admission. A connection's end
  is not a stop.
- **Sleep or a dropped connection**: a started run goes on. `qk remote` reconnects and collects
  the log, result and returned files from the journal the other Mac keeps under `dispatches/`; a
  stop asked for meanwhile reaches it on the next connection. The receipt's `connection` says so.
- **Exceptions**: a run carrying a device reservation, whose record is this Mac's, and one whose
  dispatcher had not yet heard it start. For these the dispatcher writes a heartbeat every 20 s and
  the other Mac stops the run after `HEARTBEAT_SILENCE` (180 s), longer than ssh's two-minute
  bound.
- **A dispatcher that died** looks like sleep from the other side, so this Mac decides: each
  dispatch records itself under `dispatchers/` (`remote.dispatch_records`), and the next dispatch
  or worker start sends `stop` for any whose dispatcher is proven gone. A live one is left alone.
- **Nobody attending**: past the lane's timeouts plus 95 minutes for a checkout and admission, the
  other Mac stops what is left of a detached run, and SIGKILLs it five minutes later. A run that
  returns nothing within that bound of this Mac waking is unmeasured.

ssh's keepalive (`ServerAliveInterval` 30 s, four misses) closes a silent connection after about
two minutes. A failure report compares a clock that runs through sleep with one that stops, to
say how long this Mac slept.

### Keeping the Macs alike

`qk doctor` compares the Macs (`cli.fix_hosts` holds what it fixes): default Xcode, the Xcode at
this Mac's developer directory, simulator runtimes (evened out as `qk remote setup --runtimes`
does, an error only when that fails), refused logins, and per Mac a `connection` record of each
resolved address and its connect time. It shuts down stale pool simulators on each Mac that
answered and lists processes earlier runs left there. An unreachable Mac is `unchecked`.

Another Mac's own toolkit checkout, `~/Development/queuekeeper`, is used by nothing dispatched but
by anything run from it directly, such as a recovery over ssh. Each dispatch's preparing
connection, and `qk doctor`, fast-forward it to `origin/main` without hooks, only when it is on
`main`, clean, holds no commit `origin/main` lacks, is behind, and no process runs from it.
Otherwise it is left as it is and a line says why; that case is the owner's. One 40 s deadline
covers the steps, prompts are off, a lock lets one connection update at a time, and a failure
never fails the dispatch. Plugin folders beside a project, `~/Development/FOLDER` there, are
fast-forwarded the same way.

### Plugin pins

A run pins each plugin folder beside the project, and each folder its `siblings` lists, at its
`origin/main` as this Mac's checkout names it: fetched by the queue worker each round, taken as is
by `qk run`. The pin set travels in `QUEUEKEEPER_SIBLINGS` (`pins.py`); a command that finds it
set uses it, and the worker drops an inherited one and makes its own. Each pinned commit is
checked out once per Mac as a read-only detached worktree at
`~/Development/queuekeeper-pins/FOLDER/COMMIT/FOLDER`, ending in the folder's name because SwiftPM
names a local package by its folder. Another Mac is sent only the commits, takes each from its
own `~/Development/FOLDER`, and links the pinned checkout beside the dispatched one. Before a
round adopts a new set, the worker starts Queuekeeper with it at `origin/main`; exit 70 keeps the
last set that started. Without the pin, a landing mid-run could reach later lanes with an adapter
that no longer matches.

A plugin folder inside the project is not pinned: `config.plugins_of` resolves it against
the main checkout, so no command's hooks are an unlanded copy, and `qk unittest` sets it aside while
it discovers the tree under test's own modules. A round therefore judges a change to that folder
by main's copy, as `qk land` judges an entry by main's `queue.preflight` and the worker runs the
pre-push hook an absolute `core.hooksPath` names. None of them loads from the candidate. Instead the
refusal says so and names the two landings (vocaro/queuekeeper#186): `preflight_staged` for a lane
the entry drops or changes, `main_copies` in a rejection's facts, and `hooks_elsewhere` in the
hook's hold.

### Local Network detection

When a connection to another Mac fails, the reach check asks macOS which process is responsible
(`responsibility_get_pid_responsible_for_pid`). When it is a Python, it raises `NoLocalNetwork`
rather than reporting the Mac down or searching for a renamed one. The worker checks before each
round and exits; a gate's lane exits 69. The signed launcher fixes it by naming its bundle ID in
`AssociatedBundleIdentifiers` (Apple's TN3179 explains the rule). Tests never ask macOS:
`QUEUEKEEPER_RESPONSIBLE_PROCESS` (`PID PATH`, or other text for no answer) is set for every `qk`
the suite starts, so results don't depend on which process tree runs the suite.

### Tests never reach a real Mac

`tests/host_isolation.py` replaces `cli.fix_hosts`, and a test's farm is its own namespace's,
empty unless the test writes a fake Mac. Stand-ins for `ssh` and `dns-sd` come first on `PATH` for
the suite and everything it starts, so every connection fails as to an unresolvable name and
Bonjour lists no Mac. A test that leaves a remote call unpatched fails the same way on every Mac.
Fixtures map each plugin to its own checkout rather than rewriting every `path` to the
toolkit's.

## Admission internals

How admission, supervision and recovery work inside. The rules they serve, and why, are under
[Scheduling](#scheduling).

### The slot directory

Each Mac's admission state is in one *slot directory*, in `getconf DARWIN_USER_TEMP_DIR` under the
legacy name `pdfreflow-heavy-slots`, whatever the command's `TMPDIR`, so every process on the Mac
shares one budget. It holds the slot locks, the `model` lock (an `flock`), `exclusive-queue.json`
(the queue's arrivals), `cpu-samples.json` (the shared busy-share readings) and
`coordination.lock`, under which all of them change. An answer from `getconf` that is not an
absolute path is refused, since a relative one would start a second budget.

`HEAVY_SLOT_DIR` overrides the directory for isolated tests only. The durable files are elsewhere,
in `~/Library/Application Support/queuekeeper/`: `admission-limits.json` and the admission log. A
`HEAVY_SLOT_DIR` namespace keeps its own copies of both. `QUEUEKEEPER_TRIAL` moves all of it, and
the farm, the device record and kept builds, into one folder.

The default limits and their environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `HEAVY_SLOTS` | 3 | Guaranteed slots |
| `HEAVY_SLOTS_MAX` | half the CPU count | Most slots in all |
| `HEAVY_BUSY_LIMIT` | 90 | CPU busy share, in percent, at or over which only the first job is admitted |
| `HEAVY_SETTLE_SECONDS` | 30 | Wait after a slot is taken before another beyond the guaranteed ones |
| `HEAVY_QUIET_LIMIT` | 25 | Busy share exclusive work waits to fall under |
| `HEAVY_QUIET_SECONDS` | 300 | Longest exclusive work waits for that; 0 starts at once |
| `HEAVY_PLACE_SECONDS` | 180 | How long a queued place holds others back without renewal |
| `HEAVY_RECOVER_SECONDS` | 60 | How often admission rechecks a stranded entry |

The first four can be set in the limits file; the quiet limits are environment variables only.
A new key in the limits file is a breaking change: an older toolkit copy refuses every request on
that Mac when it reads one, so write a new key only once nothing on that Mac runs an older copy.

The busy share comes from the kernel's tick counters over about 15 seconds. A request's first
five seconds of samples withhold a slot as `settle`; while the share can't be read, a guaranteed
slot opens anyway. Memory pressure is `kern.memorystatus_vm_pressure_level` (1 is normal). The
one-minute load is logged but decides nothing, since it reads high (27 to 66) on a half-idle Mac
running simulators.

### Queue order

Every request records its arrival in `exclusive-queue.json`. A queued request holds no slot.
Admission drops a record whose process has ended or whose PID has another start time, and a stop
removes its own. Order never creates another resource: a free checkout, an unlocked screen or the
model lock is still needed.

- **Arrival order holds across kinds.** An older one-slot request runs before a later whole-host
  or exclusive one; one-slot requests share slots with each other.
- **An older exclusive request** holds every later request back while the slots drain.
- **An older whole-host request** holds back less: later work may take slots beyond the
  guaranteed ones while the Mac has room, but none of the guaranteed slots.
- **A place must be renewed.** A request holds anything back only while its process keeps asking,
  or says its work still waits; after `HEAVY_PLACE_SECONDS` it holds nothing back but keeps its
  arrival for its next attempt.
- Records carry `queuedAt`, `seen` (the last attempt), `asked` while an offer is under way, and
  `ordinary` or `wholeHost` to say their kind. An older toolkit reads an `ordinary` record as
  exclusive, so upgrade every running copy together.

**Keeping a place across offers.** `qk run` and `qk admit` keep one place across local waits
between offers to other Macs, so a lane doesn't rejoin behind later arrivals. A dispatch carries
the request's age, translated to the receiving Mac's clock. Capacity preflight ignores exclusive
requests that arrived later. During an offer, `qk run` renews its place every third of
`HEAVY_PLACE_SECONDS` while its wait record shows no other Mac has started the lane
(`resources.Place.kept`, `lane_wait.taken_there`). Independently admitted child work, such as each
part of a `self` lane, starts with a fresh age.

### A whole-host lane's places on other Macs

Once a whole-host lane's first wait here ends unadmitted, `qk run` starts a *holder* over SSH on
each other Mac of the farm that may run the lane, and says it holds a place in that Mac's queue.

- **A place** is a queued whole-host request in that Mac's `exclusive-queue.json`, labeled
  `LABEL (waiting, sent by NAME)`, owned by the holder. It claims no checkout and holds no slot.
- **Renewal.** The sender writes to each holder every third of `HEAVY_PLACE_SECONDS`. When the
  sender ends, the connections close and holders remove their places at once; a silent sender's
  place lapses after `HEAVY_PLACE_SECONDS`, and its holder ends after twice that. An unreachable
  Mac holds no place and is retried at each renewal. A Mac a probe missed gives up its place, and
  takes it again once a probe reaches it, since it isn't offered the lane until then.
- **Starting.** The sender reads the Macs every 30 seconds at most (`PLACED_WAIT`). A Mac doesn't
  count the lane's own place as ahead of it; the lane is offered there, carrying
  `QUEUEKEEPER_ADMISSION_PLACE` so its `provision` lane and the lane itself are admitted past it.
- **Never twice.** One process starts the lane, in one place at a time. Admitted here, it gives up
  its other places before its command starts; started elsewhere, within one renewal.
- **Screens.** A lane that needs an unlocked screen has a place on a Mac only while that Mac's
  screen reads unlocked (`unknown` counts as locked), checked every 5 seconds.
- **No place** is taken by exclusive work (it joins another Mac's queue only after 900 seconds of
  declined offers), one-slot work, `--if-free` requests, or a lane that stays here.

### Nested admission

Nested work reuses its inherited admission and never takes a second slot. Admission exports
`QUEUEKEEPER_RESOURCE_TOKEN` and keeps its lock descriptors open across `exec`. A nested `qk run`,
`qk admit` or project gate validates them against the lock files, the owner's nonce, its guard
entry and its process start, then runs inside the outer admission without waiting behind a queued
exclusive request. An exclusive request inside shared admission is refused, as is a model request
inside an admission that doesn't hold the model.

The process supervisor and `qk unittest` pass a child only the descriptors their own process
holds. A child started by a plain `subprocess` call keeps the token but not the descriptors, so a
nested admission there is refused rather than given a slot. A process wrapper must forward the
descriptors; copying the token's environment alone transfers nothing.

### Process supervision

`processes.supervise`, which `qk run`, `qk admit` and the resource CLI use, makes sure nothing a
command starts outlives it unseen.

- **Starting.** The command starts in a new session and process group. The supervisor captures the
  root's start identity (`ps -o lstart=`) before `poll()` can reap it. A command that cannot start
  raises `NotStarted` and releases admission.
- **Signaling.** It signals the group and each observed descendant by PID, checking each one's
  start identity, since Swift drivers start other groups and sessions. A refused signal never ends
  supervision: Darwin answers `killpg` with EPERM when the group's only member is an unreaped
  root. An unknown or reused PID is never signaled. A process first seen after the first signal
  gets it once, unless its parent is an admission owner, which forwards the stop itself.
- **The grace.** Once the root exits, its descendants have 60 seconds, then SIGTERM and SIGKILL
  10 seconds later (120 seconds for a foreground run, which restores the Mac first). Programs
  inside an Xcode bundle that launchd adopted, other than `xcodebuild`, get no grace. A lane's
  `timeout` bounds only the root.
- **Stops and timeouts.** SIGTERM, SIGINT and SIGHUP are a stop. Once admitted, the job first marks
  its guard entry uncertain, in case SIGKILL follows, then forwards the signal and sends SIGKILL
  10 seconds later. A later signal doesn't hasten SIGKILL, since a task stop can deliver SIGTERM
  twice. A timeout is carried out the same way and raises `TimedOut` (exit 3).
- **Nested owners.** When SIGKILL falls due and an observed process is an admission owner, the
  outer supervisor waits up to `SETTLE_WAIT` (30 s) more for it to stop its own command and settle
  its entry.

**The final census.** A nonzero exit, a stop or a timeout counts only after a census. At each
sample the supervisor records the process group and session (`os.getsid`, since `ps -o sess` prints
0 on macOS) of each live observed process. Once all have ended, any live process in one of those
groups or sessions leaves the run uncertain. It checks groups and sessions, not PIDs, because PIDs
wrap within minutes; an ID that is now a live process's PID is skipped, since the kernel never
reuses a PID that still names a group or session. It cannot see a child that moved to its own
session and was orphaned between two samples. A zero exit has no census.

When the census finds nothing, admission is released; otherwise the entry stays uncertain for
recovery. A push keeper that supervises its own commands applies the same census to a command that
fails, and keeps its checkout's transaction lock (`"failed": true`) until someone reads why.

**Testing supervision code.** Bound stalls, not durations: start a timeout under test only once
the real descendant has published its identity, and check a bound the supervisor enforces at each
sample rather than with a wall-clock limit on the run.

**Using `Admission` directly.** The supplied commands leave admission normally and return the
child's status. A caller using `Admission` itself must leave the context before raising
`SystemExit` for a nonzero result, since an exception inside it keeps the entry uncertain. To stop
as the commands do, enter `processes.forwarding_signals()` and pass
`on_stop=admission.interrupted` to `supervise`. Run long-lived processes through `supervise`, or
record them with `Admission.observe`, since recovery sees only the record.

### The guard and the process record

Three kinds of marker hold work, each freed differently:

- **Admission's guard**, `/tmp/queuekeeper-gate-suite.lock.warm-push-guard` (or
  `QUEUEKEEPER_GATE_LOCK_FILE` with `.warm-push-guard` appended), records each owner. It is never
  reclaimed by time, only by PID.
- **The exclusive queue**, `exclusive-queue.json`, records waiting requests, which hold nothing and
  are dropped when their process ends.
- **The device record's lock**, `device-handoff.json.lock`, is an `flock` the kernel releases. A
  reservation in the record ends only by its owner's release or an attended recovery.

**The guard's format.** JSON with `"vocaroEngineering": 1` and `owners`, an object of entries by
identity. Each entry has a positive integer `pid`, a `start` string, a `label` string and an
`exclusive` flag, and may have `model`, `uncertain` and `admitted`. Unknown fields are read past.
A guard with one bad entry, or without an `owners` object, is out of format as a whole: admission
and `qk admissions recover` refuse it, never overwrite it, and nested work inside it fails.

A job leaving admission changes only its own entry: a clean exit, or a stop or timeout that saw
everything end, removes it, and the guard with its last entry; anything else marks it uncertain.

**The process record**, `<guard>.IDENTITY.jsonl`, lists what supervision saw of an owner's
commands. Admission writes its first line under the coordination lock before publishing the entry,
and gives up its slots if it can't: `admission`, `owner` as `[PID, start]`, the owner's `group`
and `session`, its `ancestors`, and `supervised`. Supervision then appends, per command, `argv`
before it starts, `root` once started, the `processes` and `spaces` (groups and sessions) each
sample first sees, `closed` times for spaces, and `settled` once the command is confirmed ended.
Nested work in another process writes nothing. A line that can't be written deletes the record, so
recovery never trusts an incomplete one. Callers that supervise their own commands append the same
lines through `Admission.observe`.

### Lock reclamation

**An ended owner.** When admission can't confirm an entry's owner still runs (its PID is gone,
runs another process, or its start can't be read), it frees the entry at its next attempt
(`resources.free_holder`). It sends SIGTERM, then SIGKILL 3 seconds later, to the owner and to each
process its record names and everything below them, each only while its PID still has the recorded
start, and never to itself or its ancestors. A root recorded before its start was written counts
with the start it has now, when that falls between the owner's start and the record's last write.
It then removes the entry and its record, frees the slots and the model, replaces any lock file
something still holds, logs a `reclaimed` line and prints `heavy: reclaimed …`. Uncertainty about a
holder never keeps a slot held: jobs can be rerun, and the queue moving matters more.

**An uncertain entry whose owner still runs** is recovered instead, automatically or with
`qk admissions recover`, once all of these hold:

- its owner has ended (a `ps` that can't say counts as running);
- it has a whole process record naming each unsettled command's processes, or naming none for an
  owner that supervises every command;
- the final census against that record finds nothing, skipping processes born after the recorded
  close of every unsettled command in their group or session;
- for a record naming no command, nothing that started no earlier than the owner remains in its
  group or session.

Otherwise it changes nothing and exits 1 naming what still runs. Until recovered, a stranded entry
counts as the slots it held (every slot for exclusive work, and the model if held); exclusive work
waits behind any stranded entry. This repository's `.claude/settings.json` lets agents run
`qk admissions recover`, since it removes only provably finished entries. `qk admissions remove`,
which no allow rule matches, is for the rest, after a person has checked by hand.

**Tests and the real guard.** Every test module imports `tests/host_isolation.py`, which redirects
the guard and `HEAVY_SLOT_DIR`; `test_host_isolation` fails a module that doesn't. A test that
starts `qk` with its own `env=` builds it from `os.environ` or `host_isolation.reduced`, or passes
`--unadmitted`. `host_isolation` clears every `QUEUEKEEPER_*` and `HEAVY_*` variable a run sets, or
lists it in `INHERITED` with why that is safe. A variable of another name, such as a dispatch's
`DEVELOPER_DIR`, is found where the code writes it into a child's environment (`child_variables`,
which says what it cannot see) and is held to the same rule. A test that mocks `subprocess.run` and admits work
sets its own `HEAVY_SLOT_DIR`.

### The admission log

Each Mac appends one JSON line to `~/Library/Application Support/queuekeeper/admissions.jsonl`
when a request is admitted and another when it ends, plus one for a request withdrawn unadmitted
and for each recovery or reclamation. Refused attempts and nested work write nothing. A line past
8 MiB first rolls the file to `admissions.1.jsonl`. A log that can't be written never holds up
admission.

| Field | On | Holds |
|---|---|---|
| `time`, `event` | every line | UTC time; `admitted`, `ended`, `withdrawn`, `recovered` or `reclaimed` |
| `identity`, `pid` | every line | The request's nonce, pairing admission and end; the admitting process |
| `kind`, `label` | every line | `heavy`, `model` or `exclusive`; up to 160 characters |
| `slots` | `admitted`, `ended` | The slots held |
| `waited`, `held` | `admitted`/`withdrawn`, `ended` | Seconds to admission; seconds held |
| `settled` | exclusive `ended` | The settle wait's `seconds`, last `busy` and whether it read `quiet` |
| `holders`, `load`, `busy` | `admitted`, `withdrawn` | Admissions in the guard, load and busy share when asked |
| `wait` | a refused request's `admitted` or `withdrawn` | `refusals` by kind, `first` and `last` kinds, `load`, `busy` and peak `memory` |
| `stranded` | when one was counted | Each stranded entry counted |
| `outcome` | `ended`, `withdrawn` | `released`, `not-started`, `stopped`, `timed-out`, `uncertain`; or `timed-out`, `stopped`, `failed` |
| recovery fields | `recovered`, `reclaimed` | Census counts, `automatic` or `byHand` with `why`; what was signaled, survived and replaced, and `by` |

An `admitted` line with no `ended` line is still held, or was killed and a `recovered` line will
follow. The refusal kinds are those of the waiting messages. Older lines lack some fields, and may
count `load` or `legacy` refusals.

### The wait history

A lane's wait record, which `qk queue status` reads, goes when its run ends, and waits that hold no
slot never reach the admission log. So each Mac appends a line to `waits.jsonl`, beside its
admission log, each time a wait on it ends: it is cleared, becomes a wait of another kind or on
another Mac, or the run ends (vocaro/queuekeeper#233). At 4 MiB the file moves to `waits.1.jsonl`.
Only the Mac a wait is on writes it, and a failure to write prints a line and stops nothing.

Each line holds `time`, `event` (`wait`), `project` and `lane` (the outermost run on that Mac,
`provision` for the step before a sent lane), `kind` (`screen`, `admission`, `foreground`, `space`,
or `overflow`, `unreachable` or `alone` while waiting on an offer), `mac`, `start`, `end`,
`seconds`, `wholeHost`, `reason` (the last words, up to 300 characters), `ended` (`cleared`,
`became a wait for KIND` or `the run ended`) and, for a sent run, `dispatchedFrom`. A wait that goes
on under other words is one wait. `qk admissions waits --there LOG` reads a copy of the first farm
Mac's history instead of asking it.

### The timing history

`lane_timing.py` keeps each Mac's `timings.jsonl` beside its admission log, moved to
`timings.1.jsonl` at 2 MiB. `qk run` appends a `run` line (`time`, `project`, `lane`, `seconds`,
`commit`) for each passing whole run on that Mac, timed from the lane's own work, compile step
included, to its end. `qk timing stale` appends a `stale` line (`lane`, or `*`, and `reason`). A
lane's baseline is the nearest-rank p90 of its newest 20 runs after its latest mark, given at least 5.

While the lane's command runs, `processes.supervise` asks a `lane_timing.Pace` once a second
(`pace`), as it reads a stall monitor. A warning goes to the run log, stderr and `LANE.json.timing`
beside the lane's wait record. `offload_host.relay_wait` sends that file back with the wait record,
`lane_wait.relayed` writes it here, and `GateWatch` copies it into the round's account as the gate's
`timing`. Past a fail threshold the run raises `processes.TooSlow`, a `TimedOut` whose stop and
cleanup are a timeout's but which `qk run` concludes as a failure. `supervise` asks the pace before it checks the timeout, so
a sample that a loaded host stalls past both, as `ps` can, still ends the run as too slow. In a lane group,
`parallel.lanes` gives each member its own `Pace` (`group` set), concludes a `TooSlow` member as a failure
and records each passing member under its own name. The members share one `LANE.json.timing`, which `qk run`
clears as the group starts: `lane_timing.grouped` keeps each member's record under `lanes` and puts a
failure's words first. A dispatch carries this Mac's
stale marks in its plan (`timingStale`), which the other Mac reads from `QUEUEKEEPER_TIMING_STALE`.
`budgets.gate_warnings` drops entries settled before the gate's mark.

### How agents' tools stop commands

Measured with the probes below. The rule that follows for every agent is in the [user
guide](user_guide.md#deadlines-and-stopping-commands): follow an admitted command to its result,
and never stop its task to retry or end it early.

**Claude Code.**

- A command that outlasts its tool timeout moves to the background and runs on. A subagent that
  ends its turn, a fork included, leaves its background commands running.
- TaskStop sends SIGTERM to every process descended from the task's shell, then SIGKILL 1.5 seconds
  later. Its scan of descendants is abandoned after 500 ms, when it signals the shell's group
  alone, so it can miss a child in its own session. `qk admit` runs in the shell's group, so it
  always gets the SIGTERM, but a command that hasn't ended by SIGKILL leaves its entry uncertain.
- Stopping a subagent, by TaskStop or from the app, stops every command it started, background
  ones included. A subagent that ends at the usage limit leaves nothing behind.
- Interrupting a turn stops only its foreground command. Archiving or deleting a session from the
  sidebar, or quitting the desktop app, stops its background commands.

**Codex.** Its shell tool never times a command out; it returns after the requested wait and
leaves the command running under a session handle. When the session ends, the command gets SIGKILL
with no SIGTERM, so an `qk admit` there never marks its entry uncertain and its command may run on
unsupervised. `codex exec` ends its session with its turn. A Codex Desktop thread keeps its
commands past its turn; archiving the thread ends them, closing the window doesn't.

### Probing stop signals

`tools/stop-signals` holds standard-library probes for measuring what an action does to a running
command:

- `siglog.py LOG child SECONDS` logs each SIGTERM, SIGINT and SIGHUP it gets, and starts a child in
  a session of its own that does the same; each writes `exiting` after SECONDS.
- `watch.py LOG OUT SECONDS` polls every 5 ms for each process the log names, and its shell, and
  records when each is gone.
- `detach.py COMMAND …` starts a command in its own session under launchd, out of reach of a stop.
- `report.py DIR` prints each probe's signals and ends in seconds from the first signal.

```sh
K=~/Development/queuekeeper/tools/stop-signals D=/private/tmp/stop-probe/NAME
mkdir -p "$D" && python3 -I "$K/detach.py" python3 -I "$K/watch.py" "$D/probe.log" "$D/probe.watch" 1800
python3 -I "$K/siglog.py" "$D/probe.log" child 300 && true    # run by the tool under test
python3 -I "$K/report.py" "$D"                                # once the action is done
```

`&& true` keeps the shell as the probe's parent, as Claude Code's Bash tool has it. In the report,
a process signaled that went without `exiting` was killed after the signal; one gone with no
signal was killed by SIGKILL alone; a `session-child` that ran to its end while its `child` was
signaled was missed; and both running to their end means the action stopped nothing. A stop or
quit in the Claude desktop app is the owner's to perform; the session starts the probe and reads
the report.

## Scheduling

Scheduling here means admission on each Mac, placement across the Macs and the merge queue's
order. This section states what they are for, the rules in force with the reason for each, and
the choices behind them. Read it before changing admission, placement or the queue's order,
and weigh a proposal against these goals rather than against the latest incident.

### Goals, in order

Scheduling optimizes for throughput without starving jobs, respects each Mac's CPU limit, balances
load where possible, and treats every Mac alike except for its own settings. Where two goals
conflict, the earlier wins:

1. **A verdict is true.** A pass, failure or timing describes the commits, never what else the
   Mac was doing. Work that could not be measured runs again rather than passing or failing.
2. **Each Mac stays within its own limits.**
3. **Nothing waits without bound.** Requests are served in arrival order, and later or lighter
   work does not pass a request that has waited.
4. **Landings flow, and no Mac idles while work waits.** This is the objective the first three
   bound: entries landed per hour, across every project.
5. **A session's own run starts soon**, served by spare capacity, never by priority.

Two rules stand beside them. **The owner is not the scheduler**: Queuekeeper chooses the Mac and
the moment, and a queue holds for the owner only over a cause only the owner can fix. **No Mac is
special**: Macs differ only by their own settings (CPU limits, which kinds of work each takes and
which labels each has). In one sentence: land as much as the Macs can verify truthfully, within
each Mac's limits, in arrival order with bounded waits, and only then make a single run start
sooner.

### Resources

| Resource | Per | How much | Taken by |
|---|---|---|---|
| Guaranteed slot | Mac | `slots` (3) | Any admitted work under `busyLimit`; the first job at any reading |
| Slot beyond them | Mac | up to `slotsMax` (half the cores) | One-slot work, one at a time, under `busyLimit`, normal memory pressure, after `settleSeconds` |
| Whole host | Mac | one lane | A whole-host lane: every guaranteed slot, nothing else admitted beside it |
| Exclusive host | Mac | one request | Exclusive work: every slot and the model, once the CPU reads quiet or after 300 s |
| Model and GPU | Mac | one run | A model run, with one slot |
| Unlocked screen | Mac | a state | A lane with `"unlockedScreen": true` |
| Foreground | Mac that takes it | one holder | A foreground lane, taken before admission |
| Device, pool simulator | device; project and Mac | one holder each | Device and simulator lanes, under a reservation |
| Checkout | project, on another Mac | four | Any run sent there |
| `main` | repository | one worker, one batch, one stacked gate | A queue's entries |

`busyLimit` bounds when work starts, on a Mac and in placement, and when a worker reruns after no
verdict. It doesn't bound what work does afterward: a slot holds a lane however many processes it
starts, nothing is preempted, and load that never asks is not counted, though it delays every
admission but the first.

Admission knows a request's kind, arrival and label, not who asks: a queue's gate and a session's
hand-run lane are requests alike, and two one-slot requests count the same however many cores
each uses.

### Rules in force

**On one Mac.**

1. All heavy work asks one queue per Mac; a private pool is not allowed. *Work that skips the queue
   can overload a Mac and starves the work that asks.*
2. First come, first served, with no class by size or by who asks. *A class that goes first starves
   what it passes, and admission can't tell how long work will run.*
3. `busyLimit` bounds every admission but a Mac's first job. *A fixed number of slots leaves a
   Mac idle or overloaded, so the load reading decides whether there is room. The first job is kept
   so load that never asks can't stop all work.*
4. Each Mac sets its own limits; raise `slotsMax` and `busyLimit` to load a Mac more, not `slots`.
   Setup writes 70% for its Mac; the default, which an added Mac keeps, is 90%. *Each guaranteed
   slot added makes a Mac worse for gates.*
5. One whole-host lane per Mac, with nothing admitted beside it. *A gate's load dips between
   phases, and work admitted on a dip would leave the gate unmeasured.*
6. A waiting whole-host lane keeps the guaranteed slots from later arrivals, on every Mac where it
   holds a place (rule 19). *Otherwise later work takes each slot as it frees, and the gate can wait
   until it times out.*
7. An older one-slot request holds back a later whole-host or exclusive one. *Arrival order holds
   across kinds* (D2).
8. Exclusive work takes every slot and the model, one request at a time, and starts once the CPU
   reads quiet or after 300 s. *A timing is void beside other work or its trailing reading.*
9. Only a timed part is exclusive; a gate suite never is, and a self-voiding check doesn't belong in
   a shared gate. *An exclusive gate would hold a whole Mac for the length of a suite.*
10. One model or GPU run per Mac, with one slot. *The hardware runs one; holding the host would
    stall builds for nothing.*
11. A request keeps its arrival and place while offered to other Macs; a place not renewed for 180 s
    holds nothing back. *Otherwise a request offered elsewhere loses its turn here.*
12. Nothing is preempted, and a wait ends at 5,400 s with nothing run. *Stopping admitted work loses
    it; the cost is that later work can void a run (#28).*
13. An owner that has ended, or can't be confirmed running, holds nothing. *Otherwise one stranded
    entry would hold back everyone's work.*

**Across the Macs.**

14. Queuekeeper chooses the Mac: of those with room, this Mac included, the one with the most
    headroom (its `busyLimit` less its busy share); the farm's order between equals, this Mac after
    the listed ones; whichever admits first when none has room. *Each Mac's limit says how much it
    is to be loaded, so load spreads* (D5).
15. Every lane is offered unless it says `here` with a reason; a reason that is a property of a Mac
    is a label instead. *A difference between Macs is a defect to fix.*
16. A Mac has room by its own admission's rules, one test for every Mac. *An offer where admission
    would hold it costs a checkout and a wait.*
17. Exclusive work sent to another Mac is offered to start at once, and joins that Mac's queue only
    after 900 s of declined offers. *A queued exclusive request idles the Mac.*
18. An unreachable Mac is passed over; a misconfigured one runs nothing and stops for the owner.
    *A different Xcode gives results that can't be compared.*
19. A waiting whole-host lane takes a place in the queue of every Mac that can run it, runs on the
    first to admit it and gives up the others. A lane that needs an unlocked screen holds a place
    only where the screen reads unlocked. *Otherwise gates queue for one Mac while another sits
    idle* (D6).
20. A lane that needs an unlocked screen goes only to a Mac whose screen reads unlocked, holding no
    slot while it waits; a locked screen holds back only that work on its own Mac. *A run on a locked
    screen can't be measured.*
21. One foreground holder per Mac, only on Macs whose settings take foreground work. *The screen is
    the owner's until the owner says otherwise.*
22. Devices and pool simulators have one holder each, under a reservation; the owner's phone needs
    the owner's word first.
23. A Mac takes only the kinds of work its `mac-settings.json` allows, and a lane runs only on a Mac
    with every label it needs. When no Mac takes a lane, it stops for whoever sets what Macs take.
    *Preferences and licenses are facts about a Mac, not rules naming one* (D7).

**In a merge queue.**

24. One worker per repository takes everything waiting, oldest first, as one batch with one gate.
25. A failed batch tries failed tests alone, then one suspect entry, then a bisect. *A bisect costs
    about a gate per halving.* When both halves pass, a search gates subsets for the fewest entries
    that fail together and sends each back naming the others, and an entry that fails alone after
    the bisect landed its partner names that partner. *An agent told its partner can rebase onto it;
    one blamed alone cannot tell why it failed.*
26. A bisect's halves gate at once on two different Macs, and entries arriving during a gate are
    gated on its tip on any Mac but the one that admitted it. *The next gate is under way before this
    one lands.*
27. A docs-only entry never rides a code gate, and passes an older entry only when they share no
    path.
28. The worker runs preflight lanes and the docs check without admission; one that runs past a
    minute runs again admitted. *Checks that take seconds should not wait behind gates for slots.*
29. A run with no verdict blames nobody and reruns once the Mac settles; three in a row hold the
    queue, not counting up to five voided by a locked screen.
30. `--front` and `--no-gate` are for emergencies, with the reason recorded.

Rules 27, 28 and 30 are the only exceptions to rule 2, and none gives a class of work priority in
admission.

### Choices behind the rules

The rules above cite these by their labels.

- **D1, the order of goals.** With limits above bounded waits, a Mac kept over its limit by work
  that never asks delays everything but its first job, and a request there can time out with
  nothing run.
- **D2, a gate does not pass one-slot work queued ahead of it.** Arrival order holds (rule 7). Only
  a small share of gate waiting is behind older one-slot work, too little to give up goal 3 for.
- **D3, hand-run lanes keep their guaranteed slots while a gate waits.** Reserving slots for gates
  would make them a priority class and starve hand-run work on Macs over their limits. While slots
  keep gates out, the Mac is nearly always at its CPU limit: the holders are using it, and the
  shortage is Macs for gates (D6).
- **D4, a Mac's CPU limit bounds every admission but the first** (rules 3 and 16). This makes the
  limit mean what it says, and it is safe only with D5, or an over-limit Mac would push all work to
  this one.
- **D5, load is balanced by headroom against each Mac's own limit** (rule 14), so one Mac's
  guaranteed slots don't fill with one-slot work while every gate goes to another. A 15-second
  reading can send several placements to one quiet Mac, and leaving a warm checkout costs a colder
  build.
- **D6, a waiting gate takes a place in each Mac's queue** (rule 19). Most gate waiting is one
  gate behind another, and places drain every Mac toward a waiting gate with no new priority; the
  effect reads as each Mac's whole-host share in `qk admissions usage`. A Mac runs one gate at a
  time: two on one Mac overload it and leave most gates with no verdict.
- **D7, no Mac is special.** Setup is the one exception: it writes 70% for its own Mac, and an
  added Mac defaults to 90%. This Mac is a placement candidate like any other, with one test of
  room, either half of a queue's pair on any Mac, the foreground only where a Mac's settings allow
  it, and kinds plus labels in each Mac's `mac-settings.json`. Devices are declared with
  `qk devices declare` (their `reach`, in order, and whether being `absent` is an error or
  offline) and read by `qk devices status`.

### Weighing a change

A proposal to change a rule says which goal it serves, which it costs, what bounds the cost, and
which reading would prove it wrong. Three readings exist: `qk admissions usage`, for each Mac's
capacity, limits and share of time at each count of admitted jobs; each Mac's
[admission log](#the-admission-log), for every wait for admission with its reason and the load it
saw; and `qk admissions waits` over each Mac's [wait history](#the-wait-history), for the waits
that take no admission, such as for an unlocked screen.

## Device, simulator and foreground internals

How the platform plugins keep their reservations and guards. The
[user guide](user_guide.md#apps-devices-and-simulators) covers using them. Code lives in
`plugins/mobile/src/queuekeeper_mobile/` (the device record, bounded runs, the device pass, reach),
`plugins/apple/src/queuekeeper_apple/` (simulators, VoiceOver state, device notifications, kept builds)
and `plugins/mac-desktop/src/queuekeeper_mac_desktop/` (the foreground, crash alerts, Notification
Center, opened apps, capture approvals).

### The device record

`queuekeeper_mobile/devices.py` is the record's only writer. It finds the record from the account's home
directory, not `HOME` or `TMPDIR`, so a session with a private environment joins the same record.
`QUEUEKEEPER_DEVICE_RECORD` names an isolated record, for tests only. Committed documentation keeps no
device identifiers or live session details.

- **A coordination record, not a mutex.** Each change takes an exclusive lock, reads the record fresh,
  refuses unless the entry is at the caller's revision (and, for an update or release, still the
  caller's), and replaces the file whole without touching other entries. The lock covers one change,
  never a reservation. Scripts use `devices.Record()` and its `reserve`, `update`, `release` and
  `recover`.
- **An entry records** the device and owner, its revision, its state (reserved, testing or released),
  whether a run is active and the process whose end ends it (`hostProcessIdentifier` and start time),
  the purpose and expected release, original settings and what to preserve, launched apps, and at
  handoff the outcome, restoration and release time. A released entry's apps move to `previousLease`.
- **Beside the entries** it keeps `mac-foreground`, the owner's notification setup, the `queues` that
  `take` waits in (so joining a line never changes the holder's revision; a place whose `take` process
  is gone drops out), and `declarations`. Reach readings go in `device-reach.json` beside it, never in
  it.
- **No approval.** The record schedules devices and `mac-foreground` first come first served, and
  refuses `--approved-*`. `devices.require_window` stops work under a reservation whose recorded
  approval has ended.
- **Legacy records.** The helper refuses a record in `/private/tmp`, where entries can be lost. `qk devices import PATH… --by SESSION` merges one once, by its SHA-256:
  a held version wins over a released one, the later activity between equals, the loser is kept in
  `importedVersions`, and two holds of one device leave it uncertain.
- `python3 -m queuekeeper_mobile.devices` finds its module only in a process `qk` started, which is why
  recovery messages print the full `bin/qk --project` form.

### Bounded runs and device identities

`devices.BoundedRun` (and `qk devices run`, `qk remote --device` and the device pass, which use it) marks
the entry's run active with its own PID, records the command's process, and records the outcome once the
command and its observed descendants have ended. An end it cannot confirm leaves the run active and the
entry uncertain. It updates only its own entry; a change the holder makes mid-run, such as extending its
window, does not stop the run's end being recorded, since it rereads the entry while the same owner holds
it with this run active.

**Automatic recovery** (`queuekeeper_mobile/upkeep.py`, vocaro/queuekeeper#230). A run killed outright
records no end. A physical device's orphan is recovered without a session asking once its end is proven:
`hostProcessIdentifier` no longer runs, or runs with a start other than `hostProcessStart` (or, with no
start recorded, one after `run.startedAt`). Recovery stops each of `run.processes` (the command's PIDs,
which `BoundedRun.own` records) still running with its recorded start, by its process group, runs the
platform's restore when `restore: pending`, and clears `activeRun`, appending to `runRecoveries`; the
reservation stays held. An entry without `run.processes` but with a live owned process, a run with
`run.host`, an uncertain entry, or a failed stop records `runRecoveryFailed` with the step and the
`devices recover` command, and is not tried again for the same run. A failed restore leaves `restore:
pending` and does not stop the recovery. The same module puts back notifications left pending 15 minutes
(`QUEUEKEEPER_DEVICE_RESTORE_GRACE`) past a held entry's expected release, recording `silence: put back`
and `putBack`, and tells the owner by a banner (`queuekeeper_mac_desktop/owner_notice.py`); a failure is
retried after ten minutes.

**Where upkeep runs.** There is no timer of its own. `plugins.HOST_COMMAND` calls `upkeep.spawn` before
every command that reaches the hosts: it reads the record, and only when something is due, at most once a
minute, starts `python -m queuekeeper_mobile.upkeep` apart from itself. The queue's signed launcher runs
such a command every two minutes, which is what bounds how late an overdue restore runs on a Mac with a
launcher. `devices status`, `doctor`, a waiting `take`, `BoundedRun`, `devices release` and the device
pass call it inline for the entries they touch, so the next pass never meets an orphan its run left. An
flock beside the record (`device-upkeep.lock`) lets one upkeep act at a time. Tests switch the spawn and
the banner off (`QUEUEKEEPER_DEVICE_UPKEEP`, `QUEUEKEEPER_OWNER_NOTICE`).

The bound's 105 seconds cover supervision's grace, SIGTERM, settling and SIGKILL waits. An empty test
run is unmeasured because `xcodebuild` has reported `** TEST EXECUTE SUCCEEDED **` after `Executed 0
tests`.

**Which hex strings name a device.** A hardware UDID is 8-and-16 hex digits, or 40. A simulator UUID and
longer runs, such as a SHA-256, name none. Nor do 40 digits that the current directory's Git knows as an
object (`git cat-file -e`), or that follow a signing setting (`…IDENTITY=`, `--identity`, or `-s` and
`--sign` in a `codesign` command). A device's name or CoreDevice identifier is not recognized, so devices
must be named by UDID.

### Devices across Macs

- **Deferral.** `qk devices defer MAC` records `physicalDevicesRecordedOn`. Only `--replace` changes the
  Mac and `--end` ends it; a record that already defers elsewhere, or holds a device, stops a `qk remote`
  run as an owner's configuration problem. The setup runs in the toolkit the sending Mac sent. Only the
  record's own refusal (status 2) is reported as configuration; a toolkit lacking the module is a version
  difference.
- **Another Mac never reads this Mac's record**, which would give code there a way in. A run carries
  its lease in `QUEUEKEEPER_DEVICE_LEASE`: `{"record": MAC, "devices": {KEY: UDID}, "until": END}`.
- **The dispatch** marks each entry's run active with the `qk remote` process, records the connection's
  `ssh` as the entry's process, and records outcome, host, work and results directory in `run`. Its end,
  however it comes, stops the run there, so a vanished dispatcher leaves an orphan here. The time left
  and cleanup allowance are reported before sending and when the work starts, with an advisory warning
  when cleanup plus `--time-limit` or the lane's `timeout` exceed them.
- **Recovery after a remote run** checks that host for a process whose command line names the UDID, or
  is left over in a checkout or the temporary directory since the run started. `offload_host.py`,
  `xcodebuild` and `devicectl` all name it; a process that reaches the device without naming it is not
  seen.
- **`--git-commit`** verifies the full hash as a commit in that local repository before sending; only an
  exact, separate positional argument is exempt from the device checks.

### Notification watch

`device_notifications.py` (Apple) and its Android counterpart implement silencing and the watch.

- **Why shortcuts.** Nothing on the Mac sets or reads a device's Focus: `devicectl device settings` has
  no Focus, and the notification-settings profile needs a supervised device. `devicectl device process
  launch --payload-url shortcuts://run-shortcut?name=NAME com.apple.shortcuts` runs a shortcut by name,
  so that is what Queuekeeper does. The marker in iCloud Drive's Shortcuts folder
  (`~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/silenced.txt`) is the only
  confirmation; Restore overwrites it with `off` because iOS asks to confirm every Delete. On a Mac,
  `shortcuts run` of either shortcut changes the owner's shared Do Not Disturb before failing at Go to
  Home Screen, and another Mac with no iCloud Drive cannot restore, so never run them there except the
  Test shortcut.
- **Bookkeeping.** Every silencing records `restore: pending` before its shortcut runs, so a run that
  dies afterward still restores; then `silence: requested` with `confirmed: false`, or `failed` with
  `devicectl`'s answer. Shortcuts is recorded among launched apps. `Record.release` from a script refuses
  while a restore is pending; the CLI restores first.
- **The watch.** `idevicesyslog` relays SpringBoard's lines over USB and the network into a temporary
  directory only this account can read. A network relay needs lockdown's
  `com.apple.mobile.wireless_lockdown` `EnableWifiConnections` on the device; a device never listed over the network needs it off.
  CoreDevice's own Wi-Fi tunnel is of no use to libimobiledevice.
- **What counts as watched.** A relay must have reached the device before the command started (its
  `[connected:UDID]` or a log line; each running relay gets up to 3 seconds), relayed its log, and still
  been relaying when the command ended. Relays start with `-x`, which ends them when the device goes. A
  relay that lost the device or joined late read only part of the run; with no complete relay the run is
  recorded as not watched, naming the relay and why. A banner a partial relay did see still makes the run
  unmeasured.
- **What is kept.** `notificationWatch` keeps only the banner count, the device clock at the first one,
  and the `transport` (`usb`, `network` or `usb+network`); the lines are deleted. A rerun's first run is
  kept as `rerunOf` with `measured: false`.
- **Remote runs** are watched from the sending Mac for the whole dispatch, admission wait included, since
  this Mac holds the reservation and already reaches the device, and `offload_host.py` imports none of
  it.
- **Reading SpringBoard.** A shown banner logs `Destination BulletinDestinationBanner presentable did
  appear as banner`; a Focus-silenced one logs `Banner for notification … was dropped. suppressed: 1 …
  do not disturb: 1`. Both follow `Posting notification ID to destination
  SBNotificationBannerDestination`, which alone proves nothing. The recorded lines are in
  `tests/apple/fixtures/DeviceLog`.
- **Verifying on a new device.** Post the test from an app in the background (iOS shows the frontmost
  app's own notifications through a Focus), for example a Reminders reminder made on the Mac; confirm
  `device_notifications.presentations` finds the banner with Do Not Disturb off and nothing with it on;
  add the lines to the fixture if they differ; and confirm Restore leaves a Focus the owner had already
  set.
- **CoreDeviceService check.** `core_device.py` runs `devicectl --timeout 30 device info details`,
  waiting 15 seconds beyond that for `devicectl` itself, so a stuck service fails a shortcut in about 30
  seconds rather than the launch's 120.

### Device reach

`queuekeeper_mobile/reach.py` reads each Mac with `xcrun devicectl list devices --json-output`, another
Mac over SSH in its login shell. Reach is `connectionProperties.transportType`: `wired` for a cable,
`localNetwork` for the network; simulators (`sameMachine`) are left out. `tunnelState` is not reach. A
device's way changes by itself over hours, which is why the owner declares intent rather than
Queuekeeper inferring it from one reading.

Each reading by `qk devices status`, `qk doctor` or `qk queue health` updates `device-reach.json`: when a
Mac last reached each device by a declared way, and since when none has. A Mac that could not be read
changes nothing, and a record with no declarations is not read. `reach.SETTLE`, 300 seconds, is far
longer than any listing (3 seconds at most) or a Wi-Fi rejoin after sleep, and shorter than the 600
seconds after which `qk queue health` calls a gate's wait slow, so a missing device is named before the
wait is. The clock starts at the first reading that misses the device, never at the last one that saw it.

### Device pass timing

A device lane's admission estimate is the larger of `deviceRunMinutes` and the median of its five latest
whole passing runs for the same device plan, each of which must report every device passed. Filtered,
failed or unmeasured runs and older records without a verdict do not count. Without either it uses the
lane's timeout; a run with arguments or `--expect` keeps the shortest bound. Devices come from the
command's `--device` options or the lane's `devices`. Admission keeps the lane's place in the queue while
it waits for its latest start.

### Simulator pool internals

Code: `queuekeeper_apple/simulators/pool.py` and `template.py`.

- **Taking.** A member is reserved under its name, observed still shut down under the reservation, then
  reset, booted to `bootstatus -b` under a limit and handed over by UDID. A booted member, or one whose
  entry is held, is another session's. A new member reserves its name before `simctl create`, takes the
  lowest free number, and reuses the label the pool gives its kind; its first boot finishes before
  shutdown, since an early shutdown leaves crash reports. CoreSimulator can store a device before
  `simctl create` prints its UDID, so recovery goes only by exact name, and two devices of one name stop
  for inspection.
- **The reset** reserves the kind's settled template, erases the member, replaces its data folder with a
  `clonefile(2)` of the template's (instant on APFS, everything preserved), and releases the template.
  The member keeps its `device.plist`, so its UDID and the simulator tool's approval; `simctl clone`
  would give a new UDID. A copy keeps the template's absolute paths, so the pool repoints every link
  naming another device at the member's own data and logs and removes the copied LaunchServices store
  (`var/db/lsd/*.csstore`); otherwise members shared the template's app-group containers. A copy that
  still names another device is refused: a reset falls back to an erase, a clone is erased. Entries
  record `resetFrom`, `clonedFrom` or `erasedBecause`, and `keptState` for a kept take.
- **Warming a template** (`POOL LABEL template (RUNTIME)`, which the member pattern never matches): create
  and boot it, read the guest's runtime build, switch VoiceOver's sound cues off, mute it until the
  volume reads 0 twenty times running, switch VoiceOver on until the speech extension logs `Processing
  markup for voice locale`, switch it off and confirm, then list the system's voices with
  `_native/simulator-voices.swift` through `simctl spawn`, reboot and list them again. A second use that
  answers makes it settled. The voices are why: a freshly erased simulator's first voice request builds
  a database in memory, which took 285 to 344 seconds per `xcodebuild` clone at rest and over 1,900 under
  load, leaving gates without a verdict.
- **Limits.** A take warms within 300 seconds for the boot, 180 for VoiceOver and 300 per voice use; `qk
  simulator-pool template` uses 600 and 900. A template whose VoiceOver stayed cold is still cloned; one
  whose voices did not settle is cloned but never copied into a member.
- **Currency.** A template is current while simctl lists the same installed builds of its runtime as when
  it was warmed and its record holds its voices. Otherwise, or if it never finished warming, or its
  VoiceOver could not be confirmed off, the next take replaces it. The pool also reads
  `VoiceOverTouchEnabled` from the shut-down template's `com.apple.Accessibility.plist` after warming and
  before each copy, since a template saved with VoiceOver on made members whose Settings rows XCUITest
  read as empty.
- **A template another session holds** past a 60-second wait, or one that is booted, is never touched;
  the member is erased or created plainly. The template is never handed to a tool.
- **App accessibility** is `ApplicationAccessibilityEnabled` in `com.apple.Accessibility`, undocumented,
  written with `simctl spawn … defaults write` after each boot of a member whose lane asks and read back
  as `1`. It is not in the template, which serves every lane of a kind; a kept member whose lane does not
  ask has it deleted. With it, `qk simulator-tests` passes
  `TEST_RUNNER_ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS=1` so golden reads fail loudly in a process with no
  system views. `xcodebuild`'s parallel clones were not checked to carry it.
- **Hold log.** Each take, extend, release and reap of a member taken by `qk simulator-pool take` appends
  a line to `~/Library/Application Support/queuekeeper/simulator-holds.jsonl`, beside
  `simulator-reaps.jsonl` (`HEAVY_SLOT_DIR` moves both), rolling at 4 MiB, so the hour's hold can be
  sized from measurement.
- **Pruning.** `PRUNE_AFTER` (an hour) is a grace between takes, not an idle limit: every take resets
  from the template, so a kept member is no warmer than a new clone. A prune reserves the member at the
  revision found eligible, rechecks it is shut down and unchanged, deletes by UDID, highest number first,
  and records `pruned` and the release. `KIND_PRUNE_AFTER` (7 days) guards a kind's last member and its
  template; `prunable` and `template_prunable` hold the conditions.
- **Stale explicit modules.** `qk run` puts the Apple package's `_shims` first on each Apple lane's PATH,
  here and on another Mac, so `xcodebuild` and `swift` called by name go through a wrapper that removes
  the module cache named in the error (`SwiftExplicitPrecompiledModules`, `ModuleCache.noindex` or
  SwiftPM's `ModuleCache`) inside the checkout, reruns once, and records it in
  `results/stale-modules.log`. Both this and `qk simulator-tests`' own recovery are tested only with
  stand-ins so far.
- **A replaced Metal Toolchain mount.** The same wrapper watches for `unable to spawn process '…' (No
  such file or directory)` naming a tool under cryptexd's mounts (`stale_toolchain`). It removes each
  `Intermediates.noindex/XCBuildData` inside the checkout whose files name the mount's folder, the part
  with the random suffix, reruns once and records it in `results/stale-toolchain.log`. When no plan inside
  the checkout names it, or the rerun fails the same way, it writes the reason to
  `results/toolchain-fault.txt`; the plugin's `plugins.HOST_FAULTS` reader returns it, and `cli.host_faulted`
  turns the lane's failure, a failed `compileFirst` build included, into unmeasured with that reason. It is
  tested only with stand-ins (vocaro/queuekeeper#281).

### Foreground internals

- **The lease.** On another Mac the foreground is that Mac's own `mac-foreground` entry. `foreground.slot`
  reserves it as `queuekeeper PID on MAC`, records its process as the active run and sets
  `QUEUEKEEPER_DEVICE_OWNER` so drivers' checks pass. The check confirms the run by its start time,
  reading it from the kernel when a sandboxed driver cannot run `ps`. A Mac takes foreground work by its
  own settings (`reservations.takes_foreground`), never by name. The run takes the foreground, then
  admission, then clears crash alerts and Notification Center windows, then starts.
- **Waiting.** A canceled waiter checks its stop signal before each 10-second poll and ends without
  changing the holder. `foreground.GRACE`, 180 seconds, covers a late holder already ending: its command
  has 120 seconds (`processes.FOREGROUND_KILL_AFTER`) between SIGTERM and SIGKILL, and VoiceOver
  restoration settles after that.
- **VoiceOver state** (`queuekeeper_apple.voiceover.state`). `read(owner)` uses AppKit's
  `NSWorkspace.isVoiceOverEnabled`, accepting only an exact boolean. That getter follows
  `voiceOverOnOffKey`, which a VoiceOver quit through AppleScript leaves at 1, so an on also needs this
  user's VoiceOver process within a stretched 5 seconds. `set_enabled` sends Command-F5 through System
  Events only when the state differs, and before an ON key waits up to 20 seconds for a quitting
  VoiceOver to finish. Every bound stretches with the one-minute load per core, up to
  `state.STRETCH_LIMIT` (24) times; a load under one per core with the CPU 95% or more busy is doubted and
  stretched fully. Errors end with the reading used, from `state.stretch_reading()`.
- **`preserved(owner, target=None, settle=False)`** captures the state under the checked lease and
  restores it on every exit. A failed transition inside it raises `TransitionFailed`, restores under the
  same lease, and watches for `state.SETTLE` (5 seconds, stretched) so a late key is seen and undone, up
  to `state.RESTORE_ATTEMPTS` (3) times. Verified, the error carries `restored` and the lease releases;
  otherwise the entry stays uncertain. Queuekeeper's guard around a child driver uses `settle=True`. A
  quiet test passes `target=False`; a speech driver enters its own scope and calls
  `set_enabled(True, owner)` inside.
- **Recovering a session's restoration.** The entry records `voiceOverOriginal` with its lease. Over a
  lease left uncertain only by a session's unverified restoration, with the same owner and lease, a live
  host process and nothing written since, `state.recover_restoration(owner)` restores it once and records
  `voiceOverRestoration`; otherwise it raises and the entry stays uncertain.
- **Stops.** Drivers run inside `voiceover.guard.stop_signals`, which raises `Stopped` where the session
  is, so VoiceOver is restored before the probe and observer's slower end. `guard.check_banner` exits
  `guard.BANNER_STOP` (123) when VoiceOver names the Automation Running banner, and `guard.run_again`
  reruns up to `guard.BANNER_ATTEMPTS` times, with `before_again` setting the stopped attempt aside.
- **Capture approvals** (`capture_approvals.py`). macOS keys each direct-capture approval by the app's
  path in `~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist`, regardless of
  signature, and lapses it 30 days after the prompt; only an MDM restriction stops the renewal, which the
  owner declined. `stable_runner_path(PROJECT, BUNDLE)`, `stage` (a `ditto` copy, refused while one runs
  from it) and `rebind` (the xctestrun's paths) launch runners from one path per Mac. Reading the plist is
  harmless; never edit it.
- **Crash alerts** (`crash_alerts.py`). The run keeps `com.apple.CrashReporter DialogType` in
  `~/Library/Application Support/queuekeeper/crash-dialog.json`, sets it to `none`, and puts back the
  exact value (deleting the key if it was unset); the next Queuekeeper command on that Mac restores one a
  dead run left. On macOS 27.2 the setting does not actually stop the alert, so clearing does the work.
  Each look first reads `CGWindowListCopyWindowInfo`, which needs no permission, and asks System Events
  only when an alert process has an on-screen window, the list cannot be read, or 30 seconds (`RECHECK`)
  have passed; looks are 2 seconds (`WATCH`) apart, and one gives up after 60. Ten failed or repeated
  presses end a look. The receipt's `crashAlerts` records the setting, the looks, and each cleared
  alert's process, start, `stoodSeconds` and `duringRun`.
- **Notification Center** (`notification_center.py`). A window counts when it is at layer 23, visible,
  and owned by the process running `NotificationCenter.app`'s binary. Closing uses System Events'
  `Name:Close` action on an element of subrole `AXNotificationCenterBanner` or
  `AXNotificationCenterAlert`, found again by subrole and frame, after rechecking `AXIsProcessTrusted`;
  then the window list is read every 0.5 seconds for up to 5, up to three rounds. The scripts walk UI
  elements (`entire contents` lists no banner) and name variables with a trailing `_`, since `kind` is a
  System Events property. To test, run a Shortcuts shortcut that shows a notification there (`osascript`
  notifications are never shown), hold the banner with a synthetic mouse-moved event and always move it
  away, and read `~/Library/DoNotDisturb/DB/Assertions.json` before touching Focus, which is shared with
  the account's other devices.
- **Opened apps** (`opened_apps.py`). The regular apps running (`NSWorkspace.runningApplications`) are
  read once the run holds the foreground, and again after its command and guards end, only while the
  lease is still the run's. Quitting waits up to `opened_apps.QUIT_WAIT` (30 seconds) and never forces.
  `tests/host_isolation.py` sets `QUEUEKEEPER_QUIT_OPENED_APPS=off` for every test run, so the suite never
  quits a real app.

### Kept app builds internals

`queuekeeper_apple/app_builds.py`. After a gated push, `keep_app` (an `plugins.AFTER_LANDING` hook)
spawns `qk app keep --after-landing`. Each build runs through `qk admit --here` on this Mac. The store's
`source/` is a detached worktree locked with the reason "qk app's source checkout for kept app builds"
(so `qk disk` keeps it); `vocaro-engineering` beside it links to the shared toolkit, and `app.links`
links other checkouts the build names by relative path. `kept/PLATFORM/SHORT/` holds the newest two
builds with `kept.json` (commit, bundle identifier, build time). A keep holds `lock` while it builds, and
`qk app run` waits on it rather than building alongside. `prepare_steps` accepts one argument list or a
list of them, as `settings.app` and the schema's `oneOf` for `app.prepare` do; a test checks that the schema and the parser agree.

### Rules the Unity plugin keeps

- **Match an editor by its absolute project path.** A fragment such as `mpm/lab` also matches the main
  checkout's editor from a worktree, which once closed another session's editor and reopened it on the
  wrong project.
- **Judge the lock by who holds it open.** A closed editor leaves `Temp/UnityLockfile`; it is removed
  only when `lsof` shows no holder, and kept when `lsof` cannot answer or the path has a symbolic link.
- **Use the editor the project pins.** A newer one reimports `Library/` and rewrites
  `ProjectVersion.txt`; `qk doctor` warns when a Mac lacks the pinned version.
- **End a silent run.** A batch run whose log has not grown for ten minutes is ended with its import
  workers, since a stuck run keeps the lock.
- **Stop an orphaned licensing helper** before starting an editor, since a helper left unanswering makes
  the next editor wait 60 seconds and fail.
- **Never pass `-nographics`.** Without a graphics device Unity loads no compute shaders.
- **Rerun one test by an anchored filter, and trust no empty run.** `-testFilter` is a regular
  expression over a test's full name, so `qk unity test --filter` escapes the name and anchors it.
  A filtered run whose results do not hold that test exits 3, never 0, so a filter that matched
  nothing is never a passing rerun.

## Visibility features

The visibility features (watch, notify, the dashboard) are optional and isolated from the queue.
The [user guide](user_guide.md#watch-the-queue) covers using them.

### How features stay separate

- **A feature is a module or package of its own** in `queuekeeper.visibility`, found by name.
  Adding one changes no other file, and deleting its folder removes it.
- **A feature reads only the journal and the snapshot** (`merge_queue.snapshot(root)`, the same
  data as `qk queue status --json`). To act on an event as it is written, it adds a hook to
  `plugins.AFTER_EVENT`. The hook must return at once, and its exceptions are dropped, so a
  slow or broken feature cannot slow or stop a round.
- **A feature imports the core and never another feature**, and the core imports none of them.
  An architecture test enforces this.
- **A feature that is off is never imported** and adds no command. One that fails to load is
  reported and left out.

### Writing a feature

A feature is a module `queuekeeper/visibility/NAME.py` or a package `queuekeeper/visibility/NAME/`
with a docstring whose first line says what it does, and a `register()` function that adds its
commands as a plugin does. It reads the queue through `events.read`, `events.follow` and
`merge_queue.snapshot`.

The three features are the examples:

- **watch** is one file.
- **notify** has rules and channels. A rule is an entry in `rules.py`; a channel is a module in
  `notify/channels/` with `ready(config)` and `send(notice, config)`.
- **dashboard** has panels. A panel is `panels/NAME.py`, with `TITLE`, `ORDER`,
  `data(repository, query)` and optionally `everywhere(repositories, query)` and `summary`, and
  `panels/NAME.js`, which draws what they return. A panel that fails shows its fault in its own
  tab while the others go on. The server answers only GET, binds `127.0.0.1`, and answers only a
  request that names this Mac, so another site cannot read the queue through a browser.

Admission names in the dashboard use the same `qk7318-description` format as queue entries, kept
in `dashboard-admission-ids.json` beside the dashboard's repository registry.

### The event journal

The queue appends one JSON object per line to `events.jsonl` in the repository's Git directory
each time its state changes. The queue only writes it, and a journal that can't be written never
stops a round. It rolls over at 4 MB, keeping the previous file as `events.1.jsonl`.

Every event has `v` (the schema version, 1), `id` (unique in the journal), `time` (seconds since
the epoch) and `kind`, plus its kind's own keys. A key with nothing to say is left out. The
schema only grows, so readers ignore what they do not know.

| Kind | When | Its own keys |
|---|---|---|
| `entry.queued` | A commit range joined the queue | `entry`, `subject`, `commits`, `branch`, `note`, `front`, `noGate`, `aheadWhy` |
| `entry.landed` | An entry is on `main` | `entry`, `subject`, `tip`, `landedAs`, `landedTip`, `landedCommits`, `docsOnly`, `ungated` |
| `entry.failed` | An entry came back from its gate | `entry`, `subject`, `tip`, `why`, `log` |
| `entry.conflict` | An entry came back with a conflict | `entry`, `subject`, `tip`, `why` |
| `entry.removed` | An entry was taken out | `entry`, `subject`, `tip` |
| `branch.deleted` | A landed entry's local branch was deleted (`landed_cleanup`) | `branch`, `tip`, `entry`, `how`, and `detached` (the worktree) for `--detach-landed` |
| `entries.testing` | Entries went under test | `entries` |
| `entries.requeued` | Entries went back to waiting | `entries` |
| `round.began` | The worker started a round | `round`, `head`, `base`, `entries`, `bisect`, `adopted` |
| `gate.began` | A lane of a gate started | `lane`, `round`, `role`, `head`, `entries` |
| `gate.ended` | A lane of a gate ended | `lane`, `round`, `role`, `status` (an exit status, `withdrawn` or `canceled`), `log`, `mac`, `seconds` |
| `gate.stuck` | A gate's wait for admission is held up by dead entries | `lane`, `role`, `why`, `waitingSince` |
| `batch.passed` | A batch's lanes passed, before its push | `head`, `base`, `entries` |
| `batch.pushed` | A batch was pushed to `main`, or a push without an answer was found there (`reconciled`) | `head`, `base`, `entries`, `ungated`, `reconciled` |
| `test.flaky` | A failed test passed alone | `test`, `commit`, `issue`, `landed`, `entries` |
| `round.together` | A search for the entries that fail together ended | `probes`, `atOnce`, `seconds` |
| `queue.held` | The queue stopped | `why`, `for` (`owner` or `session`) |
| `queue.resumed` | A hold was cleared | `was` |
| `queue.closed` | The queue was closed to new entries | `why`, `by`, `until` |
| `queue.renewed` | A closure's deadline moved, by `close --extend` | `why`, `by`, `until` |
| `queue.lapsed` | A closure passed its deadline, so the queue takes new entries again | `why`, `by`, `until` |
| `queue.reopened` | The queue takes new entries again by `qk queue reopen` | `why`, `by` (the closure that ended) |

## Checks this repository runs

- **No preflight lanes.** Queuekeeper has no spelling check of its own, and this repository runs
  none before the gate. See [Spelling](#spelling), below.
- **Docs check.** `python3 scripts/qk docs check` checks links, anchors and the `qk` commands a
  document tells a reader to type. See [Docs check](#docs-check), below.
- **The setup example.** `tests/core/test_setup_examples.py` parses each complete
  `queuekeeper.json` example in the [user guide](user_guide.md), since agents copy it. It reads
  only the code blocks whose fence is tagged `json queuekeeper.json`, which GitHub still
  highlights as JSON. Tag a whole configuration that way; tag any other JSON block, such as a
  fragment showing one setting or a `--json` result with its `schemaVersion`, plain `json`. A
  documentation-only change to the user guide runs that test through the `setup-examples` lane
  (see `queue.unread` in [queuekeeper.json](../queuekeeper.json)).
- **The plugin lists.** `tests/core/test_plugin_lists.py` fails when a platform plugin, a folder
  under `plugins/` not named `visibility-*`, is missing from `queuekeeper-workspace.json`, is not
  linked from the README's [Plugins](../README.md#plugins) section, this guide's
  [Plugins](#plugins) or the user guide's
  [Further reading](user_guide.md#further-reading), or has no box of its README name in
  `assets/overview.svg`. A documentation-only change to one of those three documents runs it
  through the `plugin-lists` lane.
- **The README's screenshots.** Retake them after changing how the dashboard looks; see
  [The README's screenshots](#the-readmes-screenshots).

### No hosted CI

The gate runs only through the merge queue, on the owner's Macs, and that is Queuekeeper's CI. No
GitHub Actions workflow or other hosted runner runs its tests or builds, and a change must not add
one. A workflow that ran `qk run gate` on a hosted macOS runner started a job at every landing,
since each landing pushes to `main`; the jobs were cancelled or failed on timing tests that pass
on the owner's Macs, and the workflow was removed
([#282](https://github.com/vocaro/queuekeeper/issues/282)). An outside pull request needs no hosted
check either: a maintainer lands its commits through the queue, whose gate tests them
([CONTRIBUTING.md](../CONTRIBUTING.md#pull-requests)).

### Spelling

Write American English. Queuekeeper itself has no spelling check: a project that wants one lists a
check of its own as a preflight lane ([preflight lanes](user_guide.md#preflight-lanes),
[#69](https://github.com/vocaro/queuekeeper/issues/69)). The American English check and its rules
belong to vocaro-engineering, as `eng spelling`.

### Docs check

`python3 scripts/qk docs check [PATHS]` checks a project's Markdown. With no paths it reads the
Markdown at the root and under `doc/`, `docs/`, `guides/`, `knowledge/`, `agents/` and `policies/`.
It takes under a second here.

- **Links.** Each link's file and anchor, and each `PATH:LINE` reference, must exist.
- **Size.** An `AGENTS.md` must stay under 32 KiB.
- **Commands.** Each `qk WORD` in a fenced shell block (`sh`, `bash`, `zsh`, `shell`, `console` or
  none) or a code span must name a subcommand of the core, the project's plugins, or any plugin
  or visibility feature in the checkout, loaded or not. Any path to the command and a `--project`
  before the word are allowed. A word in capitals, as in `qk COMMAND`, is a placeholder. A hit names the file, the line
  and the command.

**Citations are an option.** With `"docs": {"citations": true}` in queuekeeper.json, the check also
reads citations of a document by path and section title, such as `(doc/setup.md, Install)`, or by
path and anchor, in every tracked Markdown file, source file, script and `*.schema.json` (never under
an `evidence/` or `archive/` folder). Each must name a document of the checkout and begin with one of
its headings or a bold lead-in of one of its paragraphs; hits read `cites a missing document`,
`cites a missing anchor` or `cites no heading or bold lead-in of`. A citation of another repository's
document ("the toolkit's doc/...") is not read. It is off by default, and Queuekeeper itself leaves
it off: a project fixes its own hits before turning it on.

## Maintenance

### Changing Queuekeeper while work runs

- **Edit only in a linked worktree.** Do not edit, commit in or move the main checkout while a lane
  is dispatched from it. A dispatch sends a snapshot of the checkout's tree, and a run that finds
  the tree changed since does not run. Land through Queuekeeper's own queue, which moves the main
  checkout only when nothing runs from it.
- **Keep the main checkout on `main`.** Every project's launcher loads Queuekeeper from it, so a
  branch there runs that branch's code for every `qk`. When the loaded Queuekeeper is a main
  checkout on another branch, or detached at a commit `main` lacks, every command warns first on
  stderr (`queuekeeper: Queuekeeper loads from PATH, on branch NAME at COMMIT, not main, ...`) and
  `qk doctor` lists it among its warnings.

### Moving a module

Projects import Queuekeeper's modules by name, in their scripts and tests and as the module given to `python3 -m`
in a string, and each Mac's checkout takes a move on its own. A move with nothing left at the old name breaks each
project on that Mac the moment its checkout updates, whatever its queue holds (#103, #116, #213). So:

- **Leave a forwarder at the old name** for three months: a module whose body is one call,
  `forwarding.forward(__name__, "NEW.NAME", moved="DATE", until="DATE")`
  ([forwarding.py](../src/python/queuekeeper/forwarding.py)). The old name then is the new module, for
  imports, `mock.patch` and `python3 -m` alike, and warns with a DeprecationWarning naming the new one.
  [queuekeeper_apple/leases.py](../plugins/apple/src/queuekeeper_apple/leases.py) is one.
- **List it** under `forwarded` in
  [tests/architecture/module_names.json](../tests/architecture/module_names.json), which lists the module names
  of the core and the plugins. `tests/architecture/test_module_names.py` fails when a listed name is gone with
  no forwarder, and when a forwarder does not stand for its module or does not warn. Add a new module to
  `modules` there so that it is held too; a module not yet listed is no failure, since entries gated together
  often each add one.
- **A renamed function keeps its old name** the same way: a function under the old name that warns and calls the
  new one, as `foreground.second_mac` calls `farm_mac`.
- **Search the projects** for the old name, as an import and as a string, and land their changes while the
  forwarder stands. Once `until` passes and no project uses the old name, remove the forwarder and its entry.

A forwarder may only import what its own component may (see `test_queuekeeper_imports.py`), so the old
`queuekeeper.apple` and `queuekeeper.mac_desktop` names, which became plugins of their own, cannot have one.

### Renaming or retiring a setting

The merge queue judges every entry by `origin/main`'s `queuekeeper.json`, read with the strict `parse`
(`merge_queue.main_config`), and a landed Queuekeeper commit reaches every project at once: each project's
`qk land` runs the shared checkout, and each worker moves its copy to Queuekeeper's `origin/main` every round.
A release that refuses a key some project's `main` still gives makes that `main` unreadable for every entry of
that project (#295). So a `queuekeeper.json` key is renamed or retired in two landings with every project moved
between them, as `extensions` became `plugins` (#48) and `queue.unread` replaced `queue.docsOnly` and
`queue.carryOver` (#189):

1. **Accept both, and warn.** Read the new key, and the old one only while the new one is absent; refuse the
   two together; mark the old one `"deprecated": true` in the schema; and warn of it, naming the new key, in
   `qk doctor`'s warnings and in `qk land`, as `config.old_plugins_warning` did (7441b051). A key retired with
   no replacement is read as before and warned of. A module renamed with it keeps a forwarder
   ([Moving a module](#moving-a-module)).
2. **Wait for the shared checkout to hold step 1.** Every project's `qk` runs from `~/Development/queuekeeper`,
   which follows `origin/main` only while nothing runs from it. Wait for a file or line that step 1 adds, not
   for its SHA, which a rewrite of `main` would change.
3. **Move every project's `main`.** Each project lands its own change through its own queue, from a session
   started in that project. Rename only the key: a plain text replacement of `extension` also rewrote git's
   `extensions.worktreeConfig`, Apple's "app extension" and the speech extension. A project that missed one
   retirement may hold several refused keys, as one held `spelling` (#69) and `queue.docsOnly` beside
   `extensions`, so move every key the coming release refuses in one change.
4. **Confirm each project from its `origin/main`** (below), and repeat until none gives the old key.
5. **Then refuse.** Refuse the old key with a message naming the new one, and remove the fallback, the warning,
   the schema entry and the forwarder (e71d9bc6). Land it only after step 4 finds no project still giving the
   key.

**Find every project from `origin/main`, never from checkouts.** A project is a repository whose `main` has a
`queuekeeper.json`. #48's first list, from `~/Development/*/queuekeeper.json`, missed a project whose checkout
was 104 commits behind and had no such file, while its `origin/main` still gave `extensions`. List the owner's
repositories and read each `main` from GitHub, with the key in place of `OLD`:

```sh
gh repo list OWNER --limit 1000 --json nameWithOwner --jq '.[].nameWithOwner' | while read -r repo; do
  gh api -H 'Accept: application/vnd.github.raw' "repos/$repo/contents/queuekeeper.json?ref=main" \
    2>/dev/null | grep -q '"OLD"' && echo "$repo"
done
```

In a checkout, `git fetch origin` and then `git show origin/main:queuekeeper.json` read the same file. The
folders under `~/Development/queuekeeper-workers/` name the projects whose queue has run on this Mac, a check
on the list. The search matches the key's name anywhere in the file, so read each hit.

**What `qk doctor` reports.** `qk doctor` reads the working tree's `queuekeeper.json`, as every command but
`qk land` and `qk queue` does. During step 1 it lists the old key's warning; once step 5 lands it stops with the
refusal. Either way it speaks for that checkout, not for `main`: a clean `qk doctor` in a checkout behind
`origin/main` says nothing of what the queue reads. `qk land` warns of `origin/main`'s file, which it judges the
entry by, so its warning does speak for `main`, but only for a project that lands something. Confirm with the read
above, or with `qk doctor` in a checkout at `origin/main`.

**A project the scan missed is not stranded, but it is stopped.** When `parse` refuses `origin/main`'s file,
`main_config` names that file as the fault, and an entry whose own `queuekeeper.json` reads is gated by its own
file instead, less what only `main` may say (`as_batch`: `hookCoveredBy`, `unread`, `preflight` and
`affected`); `Worker.settings` reads the round's batch the same way (#295). So the fix lands through the queue,
not by a hand push. Until it does, every other entry of that project is refused; the fix's gate runs without
those settings, so every gate lane runs; and the fix must clear every key the release refuses, or its own file
does not read either. Step 1 and the scan stay the way to retire a key: this only keeps a missed project from
needing the owner.

**A branch older than the new key is told to rebase.** After step 3, `main` gives a key that Queuekeeper code
from before step 1 cannot read. Where a branch's own code reads the file, as in Queuekeeper's own repository,
whose launcher runs its checkout, an entry from a branch cut before step 1 is refused with `reasonCode`
`behind-main` and `git rebase origin/main` (`behind_main_config`, #279), since `origin/main`'s file changed
after the fork and the fork's copy reads. The user guide's
[When main's configuration can't be read](user_guide.md#when-mains-configuration-cant-be-read) says what a
project sees.

### Copies in each project

Every project carries copies of two files: `scripts/qk`, from this repository, and, in projects that
use the vocaro-engineering toolkit, its `scripts/_engineering.py`. A fix to either reaches a project only by a
landing there, a full gate each, so collect changes to them into one change and expect one landing
per project.

### The README's screenshots

The README's dashboard pictures are taken from the dashboard itself, drawing made-up data that
lives in the script. Retake them after any change to how the dashboard looks:

```sh
python3 tools/dashboard-screenshots.py
```

It needs Google Chrome and writes, in the dark theme, `assets/dashboard-home.svg` (the Queue home
view, with its Flow map animated by SVG and still where the viewer asks for reduced motion),
`dashboard-farm.png`, `dashboard-history.png` and `dashboard-activity.png`. The Trends tab's
samples are drawn at random from a fixed seed, so that they look as uneven as real work and every
retake draws the same.

**Check that the Flow map moves with `--check-motion FOLDER`**, not with two headless screenshots:
headless Chrome's `--virtual-time-budget` does not advance SVG animation, so such pictures show the
dots in one place even when the image is fine. The flag sets the image's own clock to two times,
saves a still of the map at each to `FOLDER`, and exits 1 when no dot moved and 2 when there is no
image (#237).

**Run it directly, not through `qk admit`.** It runs one headless Chrome for a few seconds and
writes into this checkout; through admission it might go to another Mac with no Chrome, so there it
refuses with exit 2. Its test, `tests/visibility/test_dashboard_screenshots.py`, does run through
admission, and fails when a panel's data no longer has the fields the script makes up. That is when
to change the script.
