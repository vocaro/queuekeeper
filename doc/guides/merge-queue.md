# The merge queue

This guide holds the merge queue's detailed rules: how a round gates a batch, what it does with
conflicts, failures, flakes and runs with no verdict, when it holds, how it pushes, and how
documentation lands. Read [Land changes](../user_guide.md#land-changes) in the user guide first;
come here when a landing does something that guide does not explain, or before changing the queue.

The queue makes one process per repository the route to `main`, as GitHub's merge queue does, on
the owner's Macs. A 45-minute gate that restarts whenever `main` moves is usually beaten by the next
landing: the owner's busiest repositories landed 8 to 14 times a day in late October 2026.

```sh
python3 scripts/qk land --wait                 # HEAD, as a background command: 0 landed, 1 came back, 75 held or still waiting
python3 scripts/qk land BRANCH                 # returns at once, and the commit lands by itself
python3 scripts/qk land --repo EPUBLib BRANCH  # from any project: land a fix in another repository
python3 scripts/qk queue status
```

## How a round works

The basics are in [How a landing works](../user_guide.md#how-a-landing-works). The rules below add
to it.

**A session does not run the full gate.** It runs the smallest relevant checks, commits and adds the
commit. Adding is the decision to land: the owner approves that once, and no later step asks.

- What is added is the range the ref has and `origin/main` lacks, held by a ref of its own, so the
  session may end and its branch may move or go. Adding a branch again replaces what it added before.
- A branch refreshed by merging `main` (the app's sync or `git merge origin/main`) is fine when each
  merge brings in only commits `origin/main` holds, with the tree a clean merge makes: `qk land`
  replays the branch's own commits onto `origin/main` in a scratch worktree, says which merge it
  dropped, and leaves the branch as it is. Any other merge, or a replay that conflicts, is refused
  (vocaro-engineering#217).
- The budget and release-limit checks after `qk land` are advisory. If their inputs cannot be read,
  `qk land` warns and keeps the queue's outcome and exit status (vocaro-engineering#355).

**A fix in another repository lands there the same way.** Commit it in a worktree of that
repository, then run `qk land --repo NAME BRANCH --wait` from wherever the session is. It joins that
repository's queue and stops none of its sessions' work. Name the commit, since HEAD there is another
session's. `--wait` prints `landed: COMMIT`, the entry's own last commit on `main` (its status line
says `-> COMMIT (N commits), in batch tip TIP` when that differs from the batch's tip). Cite that
commit, and move the dependent project's pin to it in a commit of its own, landed in the dependent's
queue.

**A round.** The worker fetches, takes everything waiting, oldest first, cherry-picks each entry onto
`origin/main` in its own checkout, `~/Development/queuekeeper-workers/NAME/NAME`, and runs the
project's queue lanes through `qk run`, in this order:

1. its preflight lanes, `queue.preflight` ([Preflight lanes](../user_guide.md#preflight-lanes));
2. its `provision` lane, when it has one;
3. its `gate` lane, or the lanes `queue.lanes` names.

The lane settings are read from `main`, or from the batch until `main` has any
([A project's first landing](#a-projects-first-landing)).

- **Default preflight.** Until a project gives `queue.preflight`, its preflight lane is its
  `spelling` lane, or the toolkit's `python3 scripts/qk spelling` for a project with none, so that a
  British spelling fails in about a minute instead of after a gate and a bisect. A later release
  removes that fallback ([#69](https://github.com/vocaro/queuekeeper/issues/69)).
  `"queue": {"spelling": false}` on `main` turns the toolkit's check off; the round's log says when
  it ran.
- **No launcher needed.** Where the checkout has no `scripts/qk`, the worker runs the check and
  `qk run` with the toolkit's own `bin/qk`, since a missing launcher once failed apple-engineering's
  first landing ([#108](https://github.com/vocaro/queuekeeper/issues/108)).
- **Placement.** Each lane goes, by itself, to whichever Mac has spare capacity, another Mac first
  ([which Mac runs a lane](another-mac.md#which-mac-runs-a-lane)); the round's log says where and why.
  Since `provision` and the gate may run on different Macs, `qk run` runs `provision` first in the
  checkout of whichever Mac runs a lane
  ([vocaro/queuekeeper#19](https://github.com/vocaro/queuekeeper/issues/19)).
- **Push.** On a pass it pushes exactly the commits it tested, as ordinary commits with their own
  messages and closing lines, so Closeout finds each branch landed by equal patches. The gate receipt
  is of the pushed commit, which `eng release` accepts, unless the result was carried over.
- **Timing.** Each landed entry records its gate's wall time, its work from admission to end, its
  wait for admission ([#112](https://github.com/vocaro/queuekeeper/issues/112)) and the CPU busy share
  of its Mac, read every minute, so the gate's budget judges its own work and its median can leave out
  runs on a loaded host ([the gate's time budget](lanes.md#the-gates-time-budget),
  [#235](https://github.com/vocaro/vocaro-engineering/issues/235)).

**The short checks run without admission.** In every round, the preflight lanes and `qk docs check`
(implicit, or a project lane named `docs` whose command is exactly the toolkit's) run in the worker's
checkout without admission, so seconds of checking never wait behind a whole-host gate. One round on
2026-11-15 waited over 40 minutes for admission to run about 2 s of checks (vocaro/queuekeeper#169,
vocaro-engineering#394).

- Each has `DOCS_CHECK_LIMIT`, 60 s. One that reaches it runs again through `qk run`, admitted, so a
  slow check is never a failure and never runs long outside admission.
- Any other docs lane, the gate, `provision` and every lane `queue.lanes` names go through ordinary
  admission.
- This holds for every kind of round: a batch, a bisect's halves, a stacked gate and a `--no-gate`
  landing. An entry that leaves the queue stops an unadmitted check as it stops an admitted lane.

**A gated push keeps the app.** For a project whose queuekeeper.json has `app`, the worker starts
`qk app keep --after-landing` for the pushed commit in the background once the push lands, and does
not wait for it. That build is what `qk app run` opens ([kept app builds](app-builds.md)).

## The toolkit a worker runs

**Which toolkit a gate runs.** Each round, a project's worker moves its own copy of the toolkit,
`~/Development/queuekeeper-workers/NAME/vocaro-engineering`, a locked worktree no session edits, to the
toolkit's `origin/main`, and runs the lanes, the pre-push hook and the carry-over checks with
`QUEUEKEEPER_ROOT` naming it (owner's decision, 2026-11-01).

- A landed toolkit commit reaches the next round of every project's gate; a lagging or edited shared
  checkout reaches none. Each worker's copy is its own, so moving it changes no other running gate.
- **The worker follows its copy.** A worker whose own code came from another commit restarts itself
  from the copy, with the same arguments, environment and bisect, before the round touches any entry,
  and logs `moving to the toolkit at COMMIT`. Reason: an older worker once refused a lane key its copy
  accepted and held the queue ([#173](https://github.com/vocaro/vocaro-engineering/issues/173),
  [#177](https://github.com/vocaro/vocaro-engineering/issues/177)).
- The toolkit's own queue tests its batch, but its worker moves the same way, to a copy no lane reads
  (`queuekeeper-workers/vocaro-engineering/vocaro-engineering-toolkit`).
- A `QUEUEKEEPER_ROOT` set where the worker started stands, and the worker does not move it.

**A worker never moves to a toolkit that could leave the queue without a worker.** Before a move it
imports the worker's code from that commit in a separate process, and the queue keeps the commit
moved to, `moved`, until the worker started there finishes a round. A commit that does not import, or
on which a moved worker died or held before finishing a round, is not moved to: the worker stays on
its own code and logs `staying on the toolkit at COMMIT, which this worker was started from, rather
than moving to COMMIT, origin/main:` with why. So a broken toolkit pushed by hand is fixed through the
toolkit's queue like any commit.

- A copy that cannot be fetched is a remote out of reach, retried as any is; one that cannot be moved
  holds the queue for a session. Neither blames a commit.
- Everything else, such as a session's own `queuekeeper`, still reads the shared checkout. A project
  commit that needs a new toolkit option lands after the toolkit's commit, and runs by hand only once
  that checkout has it.

**A worker whose session ended exits.** macOS denies a worker the local network once the Claude session
that started it ends, so it could no longer reach another Mac. While another Mac is configured, the
worker checks for this before each round, and a gate finds it by a lane's `qk run` exiting 69. Either
way the worker exits, holding nothing, with its entries back in the queue, and logs `exits, holding
nothing, since this worker cannot reach another Mac`. The next `qk land`, `qk queue wait` or
`qk queue resume` starts another worker (doc/guides/another-mac.md, Local Network access;
vocaro-engineering#282, #300).

**The shared checkout follows each toolkit landing.** After each push, the toolkit's own worker
fast-forwards `~/Development/queuekeeper` to `origin/main`, without hooks
(owner's decision, 2026-11-02, [vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)).
It does so only when that checkout:

- is on `main`, clean (no changed or untracked path) and behind;
- holds no commit of its own. A commit made there and landed through the queue is not its own: the
  worker finds its change on `origin/main` by `git cherry` and moves `main` there with
  `git reset --keep`, saying how many it moved past;
- has no process started from it still running, one whose working directory, an open file or a path
  argument is in its tree, since a run spanning the move could mix two toolkits. A worker whose code
  came from there counts until it restarts from its copy.

These do not count as a use: anything under `.claude/worktrees/` (sessions there and their servers
included, [vocaro-engineering#182](https://github.com/vocaro/vocaro-engineering/issues/182)); Git's
own directory; a non-Python program tied only by its working directory; a signed queue launcher
(`Worker.stays`, [vocaro-engineering#384](https://github.com/vocaro/vocaro-engineering/issues/384));
and a process that restarts itself when the checkout moves. A long-running one such as `qk serve`
registers in `queuekeeper-restartable/PID.json` under the checkout's Git directory and execs the new
code once HEAD moves (owner's decision, 2026-11-17: Queuekeeper's scripts and code are free to
restart as needed; [vocaro/queuekeeper#159](https://github.com/vocaro/queuekeeper/issues/159)). A
process that loaded the toolkit without keeping a path there, as a project's launcher does, cannot be
seen.

When it cannot follow, the worker leaves the checkout and logs why, such as `left
~/Development/queuekeeper as it is: it is on BRANCH, not main`. Nothing of this holds the queue.

- A checkout left only for running processes, or a failed process census, is tried again every 30
  seconds while nothing waits (owner's decision, 2026-11-11, vocaro-engineering#384). Meanwhile
  `qk queue status` and `qk queue doctor` name each of up to five holders in words, with PID and age,
  such as `the queue worker for mpm`
  ([vocaro/queuekeeper#159](https://github.com/vocaro/queuekeeper/issues/159)).
- A checkout left for any other reason is a person's to settle. A session never stashes, resets or
  edits that checkout by hand to make it follow.

**Where the queue's checkouts are.** Each queue's checkouts are in one folder beside the main
checkouts and outside every repository, laid out as another Mac lays out a run's:

```
~/Development/queuekeeper-workers/NAME/NAME                 the worker's checkout of the project
~/Development/queuekeeper-workers/NAME/vocaro-engineering   its copy of the toolkit
```

A project file that names the toolkit by a relative path, such as
`../vocaro-engineering/src/swift/EngineeringTestSupport`, finds the queue's copy with no change. A
folder inside the toolkit's checkout was rejected, since `git clean -x`, a reclone and the toolkit's
tree-walking checks would all reach it (owner's decision, 2026-11-02,
[vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)).

Older checkouts at `../NAME-queue` and `../NAME-queue-toolkit` move there by themselves with
`git worktree move`, and the worker removes the SwiftPM and Xcode package state that names the old
path; build products stay ([#114](https://github.com/vocaro/vocaro-engineering/issues/114)). When the
move cannot be made, the worker makes the checkout afresh and locks the old one as `retired by the
merge queue: replaced by PATH`, which `qk disk` lists for the owner's removal once nothing uses it.

## Checks before queuing

[Checks before queuing](../user_guide.md#checks-before-queuing) lists them. Each refusal queues
nothing and exits 1.

**A conflict is found when the entry is queued.** `qk land` and `qk queue add` make the integration
a round would make before they queue anything: onto `origin/main`, after every entry ahead that is
waiting, in its gate or passed, in the order rounds land them, with the worker's own code (its
changelog resolution included), in a scratch worktree that goes when the check ends. A `--front`
entry is tested alone, so it is checked against `origin/main` only
([#178](https://github.com/vocaro/vocaro-engineering/issues/178): before this, an entry behind a
running gate learned of a conflict 15 to 40 minutes later).

- The refusal names the files in conflict, every entry ahead it was checked against, those among them
  that change those files, and any entry ahead left out because a round would send it back too.
- It names the commit's own lines in conflict, as `PATH:FIRST-LAST`, so the author can resolve them
  before the entries ahead land.
- When entries ahead change those files, it gives the command that waits for them,
  `qk queue wait ID ID ...`, to run in the background. It ends once none of them is queued, testing
  or passed: exit 0 when each landed (rebase onto `origin/main` and land again), 1 when one came back
  (land again; it may now apply), 75 while the queue is held or at its `--limit`
  ([#304](https://github.com/vocaro/vocaro-engineering/issues/304)).
- `qk land --wait` does that wait itself when the conflict is with entries ahead alone: it waits,
  checks again against the whole queue, and queues the commit when it applies. A conflict with
  `origin/main`, alone or beside entries ahead, is refused at once, saying to rebase, since no wait
  ends it. Reason: hand-written polling loops once made one landing take 4.5 hours
  ([#302](https://github.com/vocaro/vocaro-engineering/issues/302)).
- A changelog conflict the worker would settle is no conflict.
- The check is only a hint: `main` and the entries ahead can change before the round, which
  integrates again. A check that cannot be made, such as a worktree Git cannot add, queues the entry,
  says so, and leaves any conflict to the round. It costs a second or two on a small tree, a few
  seconds on a tree of 10,000 files.

**Checked before it is queued.** Three more checks run, in this order. `--no-gate` and `--front` skip
none of them.

1. **Closing claims.** A commit message that denies a closing claim is refused, naming the commit,
   the line and what to write instead, since GitHub still closes the issue: "This does not fix #43."
   closed an issue two hours before the real fix
   ([#286](https://github.com/vocaro/vocaro-engineering/issues/286)). A claim is one `landing.CLAIM`
   matches, negated by `not`, `cannot`, `n't`, `never`, `no longer` or `without` up to three words
   before the keyword in the same clause. A clause ends at punctuation, at `and`, `but`, `so`,
   `while`, `because`, `since` or `though`, at a blank line or list item, and at a line that opens
   with a capital. So "did not start, and fixes #N", `Refs #N` and "not only fixes #N" pass, and so
   does "does not, in fact, fix #N", since the check reads text alone.
2. **Tests that cannot run.** For each queue lane on `origin/main` whose command is `qk unittest`
   (or a group's one such lane), `qk unittest`'s pre-run checks run on the entry's tree with that
   lane's `-s` and `-p`. A test module that does not parse, or that misses its top-level
   `host_isolation` import in a suite that uses it, would make every gate holding it refuse with
   `nothing ran` and bisect blind, so the entry is refused, quoting the line the gate would print
   ([#280](https://github.com/vocaro/vocaro-engineering/issues/280),
   [#290](https://github.com/vocaro/vocaro-engineering/issues/290)). A refusal `origin/main` already
   gives is not the entry's. The check reads files from the commit, not a working tree, and takes
   about a second.
3. **Preflight lanes.** After the trial integration, the preflight lanes the worker would run (the
   round's defaults above, unless `main`'s `queue.spelling` is `false`) run on the entry replayed
   onto `origin/main`, in a scratch worktree, each as the replayed commit's `queuekeeper.json` gives
   its command, with `QUEUEKEEPER_PROJECT_CHECKOUT` naming the main checkout. They run on this Mac
   without admission. A finding (exit 1) refuses the entry, quoting the lane's last 40 lines, since
   one spelling slip found by the worker cost a 40-minute round
   ([#283](https://github.com/vocaro/vocaro-engineering/issues/283)). The entry is queued anyway,
   saying why, when the check cannot be made:
   - its commits do not apply onto `origin/main` alone;
   - a lane is not one plain command (foreground, display, unlocked screen, whole host, model,
     compile first or a parallel group);
   - a run passes its timeout or 300 s, whichever is less;
   - a lane exits other than 0 or 1, such as `qk spelling`'s 2 for nothing checked;
   - `origin/main` gives the same finding, which is not the entry's.

   The worker still runs the lanes on the batch, since `main` can move in between.

## Conflicts and branches

**Lines added to a changelog section need no author.** Branches that each add a line to a section of
`CHANGELOG.md` (any directory, any case) conflict in turn. The worker settles such a conflict by
keeping both, the new lines first, and gates the result as usual
([#130](https://github.com/vocaro/vocaro-engineering/issues/130)). It does so only when:

- every conflict in the commit is in a changelog;
- each is whole lines both sides only added at one place, directly below a Markdown `#` heading or,
  when each side's lines start a list item, directly below one;
- no heading or blank line is among them, and none of the commit's lines is already in the file.

For an entry of several commits it merges the entry's whole change to the changelog, so a later
commit that rewords a line an earlier one added still counts as that line added. Anything else comes
back, naming each file in conflict.

Each settled commit is listed under `kept` in the entry's record, shown by `qk queue status` and
`qk queue wait`, and keeps its message and author. Since its patch is not the branch's, `landed.sh`
proves the branch landed as `resolved`: each branch commit has a commit on `main` with its subject and
author that adds and removes exactly its lines, file for file and hunk by hunk. Closeout then deletes
the branch like any landed one (owner's decision, 2026-11-04,
[#164](https://github.com/vocaro/vocaro-engineering/issues/164)). A resolution that dropped or changed
one of the branch's lines proves nothing, and that branch stays for its author.

**A later commit does not repair an earlier one.** The check and the round cherry-pick an entry's
commits one at a time, in order, and stop at the first that conflicts (`Worker.pick` in
`merge_queue.py`), so a branch whose final tree would apply is still refused when an earlier commit
does not (vocaro/BreezEPUB#6). Diagnose the commit the refusal names in `COMMIT conflicts with`, not
the branch's whole diff. Then:

- When the commits from that one on are yours alone, clean and pushed nowhere, amend or consolidate
  them so each applies in order, as with `git reset --soft COMMIT^` and one new commit. Rewrite no
  other session's commits or working changes.
- Otherwise wait for the entry ahead to land (`qk land --wait`, or the `qk queue wait` the refusal
  names) and rebase onto `origin/main`, resolving the conflict at that commit.

Either way, check that the new tip's tree is the one you meant (`git diff OLD-TIP NEW-TIP` empty when
only history changed), run the relevant checks again, and land it through the ordinary queue. A
conflict confined to changelog lines needs none of this.

**After it lands, start again from `main`.** The round may change a commit as it lands it, as when it
keeps both sides of `CHANGELOG.md`, so `main` holds it under another hash and patch. Begin the next
change from `origin/main`, as with `git reset --hard origin/main` on a clean branch of your own once
`qk land` says `landed`. Rebasing onto `origin/main` instead replays the landed commit and conflicts,
and merging `main` in makes a merge the queue refuses. In a rebase already under way, `git rebase
--skip` drops a landed commit.

**A branch that goes on after a landing.** The queue lands a branch's commits under new IDs, so a
branch that gets more work still holds the landed commits under their old IDs, and `qk land` refuses
it when a later landed commit changed their lines (2026-11-07, #204). Start the next change from
`origin/main`, or `git rebase origin/main`: Git drops a commit whose patch is already on `main`, and
`git rebase --skip` passes one whose change `main` holds in a later form.

## What comes back to the author

The user guide's [When a batch fails](../user_guide.md#when-a-batch-fails) gives the outline.

**What comes back to the author.** `qk queue status` and `qk queue wait` name each entry's state,
reason and log. An entry that conflicts with what is ahead of it, which the check before queuing
could not see, comes back at once and costs the others nothing. Its reason names the files in
conflict and what it conflicts with ([#247](https://github.com/vocaro/vocaro-engineering/issues/247)):

- `origin/main`, when no entry ahead changes those files;
- only the entries ahead that change them, by ID, when `origin/main` still holds those files as the
  commit's parent did. Once they land, rebase onto `origin/main` and add it again;
- otherwise `origin/main` and every entry ahead.

A session therefore lands with `qk land --wait` run as a background command, whose end wakes it
within seconds of a send-back ([#130](https://github.com/vocaro/vocaro-engineering/issues/130)). While
it waits, `qk land --wait`, like `qk queue wait`, prints a line each time its entry's situation
changes, and a `heartbeat:` line at most every 5 minutes otherwise, with what `qk queue status` shows
of that round ([#41](https://github.com/vocaro/queuekeeper/issues/41)). An entry that comes back keeps
its ref until `qk queue remove ID`; fix it, rebase and land it again.

**A send-back says the load and whether the entry changes the failed tests' files.** The reason ends
with ([#273](https://github.com/vocaro/vocaro-engineering/issues/273)):

- **The load**, of the Mac the lane ran on, in two parts: readings taken while the lane ran, which
  count its own work (a gate loads a Mac heavily by itself), and readings taken while it waited to
  start, set against that Mac's busy limit
  ([vocaro/queuekeeper#12](https://github.com/vocaro/queuekeeper/issues/12),
  [vocaro/queuekeeper#132](https://github.com/vocaro/queuekeeper/issues/132)).
- **The failed tests**, from the lane's `qk unittest` summary, each compared by its module and imports
  with the entry's commits, as for a flake below. Otherwise they come from the summary `qk run LANE:`
  ends with (`eng run LANE:` in older logs), as a Swift gate's does, each named with the file and line
  it failed at ([vocaro/queuekeeper#83](https://github.com/vocaro/queuekeeper/issues/83)). A Swift
  test's file is placed only when exactly one tracked or changed file can be it, and the commits are
  compared with that file alone.
- A test whose file cannot be placed, a summary that counts more failed tests than it names, and one
  that names none ([vocaro-engineering#406](https://github.com/vocaro/vocaro-engineering/issues/406))
  each say so, so the author knows the comparison is incomplete.

**A send-back for a quick check says whether `origin/main` fails it too.** When the failed lane is
one the round runs unadmitted, a preflight lane or the docs check, the worker runs it once more on
`origin/main` alone. The reason says whether `origin/main` passes or fails it too, how many of its
`PATH:LINE:` findings the entry's run printed as well, and that run's log. Reason: two entries once
each fixed the lane the other failed on a broken `main` and failed for four hours
([vocaro/queuekeeper#180](https://github.com/vocaro/queuekeeper/issues/180)).

## Gates running beside each other

The user guide's [While a gate runs](../user_guide.md#while-a-gate-runs) gives the outline.

**A bisect's two halves gate at once.** While a bisect goes on, the newer half of its suspects gates
beside the older half, in a second checkout, `~/Development/queuekeeper-workers/NAME/NAME-beside`, on
the same `origin/main` (owner's decision, 2026-11-07,
[#241](https://github.com/vocaro/vocaro-engineering/issues/241)), on a different Mac (owner's
direction, 2026-11-17, [#211](https://github.com/vocaro/queuekeeper/issues/211)).

- The older half is offered as any gate is (`qk run LANE`), this Mac among the candidates.
- Once a Mac has admitted the older half's final lane, the newer half goes to any other Mac, and only
  if one can start it now: with `qk run --offload-only` when the older half runs on this Mac, and
  otherwise with `qk run --if-free` and that Mac left out. The log says which (`newer half: its lanes
  go on a Mac other than build-host if one can start it now`). An older half that ends before any Mac
  admits its final lane leaves the newer half with no verdict, which queues it again.
- Whichever half passes lands, so a newer half can land first. When both pass, the older half lands
  first, and the newer lands on top only if its result carries over; otherwise it gates again. When
  both fail, the older one is bisected first.
- The rerun-alone check runs on each failed half (#231), so a half whose real failure blames one entry
  sends it back instead of splitting.

The newer half instead waits behind the older one when:

- no Mac but the older half's can start it now, or the Mac that takes it gives no verdict;
- a lane runs only on this Mac (a `self` lane, or one that says `here`);
- the round is docs-only, ungated or `--front`, or a newer entry was added with an option; or
- a newer entry applies to `origin/main` only on top of an older entry.

**Entries that arrive during a gate gate on top of it.** While a batch gates, the worker checks every
`STACK_POLL` (30 s) for entries behind it (owner's decision, 2026-11-07,
[#241](https://github.com/vocaro/vocaro-engineering/issues/241)). Once the batch's final lane has
been admitted, the worker gates them stacked on the batch's tip, in the other checkout, on a Mac other
than the one that admitted it ([#211](https://github.com/vocaro/queuekeeper/issues/211)): with that Mac
left out of `qk run`'s offer, or with `qk run --offload-only` when the batch gates on this Mac. The
stacked gate waits where any lane waits. A docs-only entry is never stacked; it lands beside the gate
([A docs-only entry never rides a code gate](#documentation-and-carry-over)).

The batch's push makes the stacked tip's base `origin/main`, so the next round adopts the stacked gate
as its own. Anything else throws it away, canceling its lanes and queuing its entries again:

- the batch fails or gives no verdict;
- the batch lands at another commit (after a carry-over, or with a `Flaky-test` trailer);
- the stacked gate gives no verdict, as when another Mac declines it;
- an entry of it is removed;
- an entry goes first (`--front`, `--no-gate`, or a pass waiting for its push);
- `main`'s lanes change;
- the queue holds; or
- the worker stops or moves to a newer toolkit.

More rules:

- A gate that uses the foreground waits its turn once the farm has Macs to choose among.
- When no eligible Mac admits the stack's next lane, the worker waits two minutes before trying
  another stack on that batch. If the batch lands first, the stack's entries go into an ordinary round
  without counting against `queue.noVerdictRuns`.
- Only one stacked gate runs at a time; later arrivals stack on it in the next round.
- A stacked gate settles nothing itself: an entry that conflicts with the batch below waits for its
  own round, and the log names what it conflicts with. A bisect does not start a stack.

**Removing an entry that is testing stops its gate.** `qk queue remove ID`, or `qk land` of the
branch again, takes the entry out at once. The worker looks every `REMOVAL_POLL` (5 s) for an entry of
its running gates that has left, stops that gate's lanes on whichever Mac, freeing its admission, and
gates the rest of the batch again with no verdict counted and no hold (owner's request,
[#247](https://github.com/vocaro/vocaro-engineering/issues/247)). Under a worker started from an older
toolkit, `qk queue remove` says instead that the gate runs on until its lanes end, at most
`LANE_LIMIT` a lane.

## Flakes, blame and bisecting

The worker tries a flake, then a single culprit, then a bisect
([When a batch fails](../user_guide.md#when-a-batch-fails)). These rules apply to `qk unittest` lanes.

**A failed test that passes alone is a flake.** Before it bisects a batch whose failing lane is
`qk unittest`, or a group with one `qk unittest` lane whose other lanes passed, the worker reruns the
failed tests alone on the same batch commit (owner's decision, 2026-11-07,
[#231](https://github.com/vocaro/vocaro-engineering/issues/231)).

- It reads their IDs from unittest's `FAIL:` and `ERROR:` lines, waits for this Mac to settle (as for
  [a retry with no verdict](#runs-with-no-verdict)), then runs each test by itself with the lane's
  `-s`, `-p`, `-t`, `--split`, `--batch-size` and `--timeout`, up to `RERUNS` (3) times.
- Any rerun that fails or ends unmeasured makes the failure real.
- When every rerun passes, each test is a flake, and the batch lands only if none of its commits
  changes the test's module or a project file that module imports (option (b)). Only the module's own
  import statements count, at any depth, looked up under the suite's top-level directory,
  `src/python`, `src` and the checkout, with each package's `__init__.py`. A batch that changes one
  bisects.
- Either way the flake is recorded in the entry's `flakes` (shown by `qk queue status`), the worker's
  log, a `Flaky-test:` trailer per test on the landed tip, and the tracker: a comment on the first open
  issue naming the test, or a new hardening issue. It quotes the test's first `FAIL:` or `ERROR:` block,
  at most `FAILURE_LINE_LIMIT` (500) characters a line and `FAILURE_EXCERPT_LIMIT` (4000) in all, since
  logs are pruned and other worktrees cannot read them (vocaro/queuekeeper#128). A tracker gh cannot
  reach is noted in the entry and stops nothing.

**A test the batch adds is no flake.** A failed test that passes alone but that `origin/main` does not
have is recorded nowhere, files no issue and does not land the batch
([vocaro/queuekeeper#129](https://github.com/vocaro/queuekeeper/issues/129)). A test is the batch's own
when its module defines its class and method in the batch and `origin/main` lacks them; one a base
class gives, or made at run time, is judged as any other.

- When every failed test that passed alone is one the batch adds, the worker sends back the entry that
  adds them (the one whose commits change the test's module, or among several, whose tip defines the
  test) and gates the rest at once.
- Any doubt bisects: no entry or several add the test, or the added tests come from different entries.
- A test of `origin/main` that passed alone beside an added test is a flake, and then no one entry
  explains every failure, so the batch bisects. Relaxing that is open in
  [vocaro/queuekeeper#88](https://github.com/vocaro/queuekeeper/issues/88).

When adding an option to a `qk unittest` gate, check the actual lane command with the runner, the
[test coverage checker](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#every-test-has-a-lane)
and the queue's single-test rerun: the first gate with `--batch-size` failed its coverage check on the
unknown option
([Batching gate results](https://github.com/vocaro/vocaro-engineering/issues/145#issuecomment-6774878492)).

**A real failure that one entry explains sends that entry back without a bisect.** When a rerun
confirms the failure of a batch of several entries, the worker reruns the other failed tests too and
looks for the one entry that explains them all (owner's decision, 2026-11-07).

- A failed test points at an entry when that entry's commits change a path the failing rerun's output
  names (as `qk spelling`'s `PATH:LINE:` lines do; only a whole path with a directory or an
  extension), or the test's module or a file it imports.
- When every failed test points at one entry, the worker confirms it: it picks the batch's other
  entries onto the gate's base, without that entry, and runs each failed test once alone. Only when
  each passes does it send that entry back, log which entry it blamed and why, and gate the rest at
  once. The reason names the batch's other entries. Reason for the confirmation: a test can read a
  file neither its imports nor its failure name, and blame once sent back the wrong entry three times
  ([vocaro/queuekeeper#183](https://github.com/vocaro/queuekeeper/issues/183)).
- Any doubt bisects: a test that points at no entry or several, tests that point at different
  entries, a module the worker cannot find, a flake beside the real failure, a rerun with no verdict,
  a test that fails without the entry too, or another entry that does not apply without it.

**A gate `qk unittest` refused sends back the one entry that brings the refusal.** When a
`qk unittest` lane's log has no summary and printed a refusal ending `nothing ran`, nothing was
tested and the same tree would be refused again, so the worker reruns nothing (owner's decision,
2026-11-08, [#290](https://github.com/vocaro/vocaro-engineering/issues/290)).

- It looks for entries that bring the refusal: one whose tree alone gives a refusal naming a module
  the line names (the check `qk land` runs), or whose commits change one of those modules in the
  lane's `-s` directory ([#280](https://github.com/vocaro/vocaro-engineering/issues/280)).
- Exactly one: it sends that entry back with the refusal line as its reason, logs it, and gates the
  rest at once. None, several, or a line that names no module: it bisects.
- When the same refusal (ignoring checkout directories) comes again in the next gate, or in both
  halves of a bisect, with no one entry bringing it, the cause is not an entry, such as a module that
  reached `origin/main` past the queue. The worker holds the queue for a session with every entry
  queued again, and the hold's reason quotes the line and `qk queue doctor`'s diagnosis. A session
  fixes the cause and runs `qk queue resume`.

**When the worker bisects at once.** It skips the reruns and bisects when:

- the failing lane is not `qk unittest` (a project's own lane, or a group not covered above);
- its log names no test it can rerun: no `qk unittest` summary and no refusal, tests left unmeasured,
  a process with no failed test named or that ran other than it was given, or a module that did not load;
- more than `RERUN_MOST` (5) tests failed; or
- this Mac does not settle within `SETTLE_LIMIT`.

Rerunning one failed test takes seconds, where a split costs every entry behind it another gate
([#230](https://github.com/vocaro/vocaro-engineering/issues/230)).

**A gate that fails on a test the change cannot reach.** On a `qk unittest` gate the queue handles
this itself when the test passes alone (above). Otherwise, record the failure on that test's issue, or
file one by the project's labels, before adding the entry again: an entry added again with no record
leaves the flake nobody's to fix ([EPUBLib#978](https://github.com/vocaro/EPUBLib/issues/978) went
through the queue four times). The options in
[Landing ahead of the queue](#landing-ahead-of-the-queue) are for a fix that cannot wait, not for a
flake.

## Runs with no verdict

The user guide's [No verdict is not a failure](../user_guide.md#no-verdict-is-not-a-failure) gives
the outline. None of these runs blames a commit.

**A lane that could not start blames nobody.** `qk run` exits 127 when a lane's command never
started: the system could not start it, or the interpreter it names (such as `python3` or `sh`) was
given a first argument that names no file in the checkout. Its receipt's `outcome` is `not-started`,
with why under `notStarted`, and its log says "could not start". `qk run` reads the file system rather
than the interpreter's exit, since `python3` exits 2 for a missing file as for a failure. The worker
reads the receipt, not the status alone, since a lane's own command may exit 127. Running it again
would not start it either, so the worker holds the queue at once for a session, with every entry
waiting: the session makes the command start, or removes the entry that took away what it runs, and
resumes ([#108](https://github.com/vocaro/queuekeeper/issues/108)). A lane group's members are not
told apart this way yet.

**A lane Queuekeeper could not start for blames nobody.** Every `qk` command first composes its
checkout's plugins and loads the project's extensions. When that raises, it prints the traceback, then
a line beginning `queuekeeper: could not start:`, and exits 70 (`EX_SOFTWARE`) before reading its
arguments ([#158](https://github.com/vocaro/queuekeeper/issues/158): before this, an incompatible
shared checkout sent at least nine entries back as failing).

- The worker treats exit 70 as this only when the run also left no receipt of the lane: on this Mac,
  no `source.json` in the lane's folder since the lane began; on another Mac, the dispatch's work
  stage exited 70 and returned no receipt.
- Before holding, it starts Queuekeeper in its own checkout with `--version`, which loads the
  extensions and runs no lane. If it starts without the batch's commits but not with them, the batch
  is at fault: it is bisected and the entry sent back, with no hold. If it starts with neither, or
  the lane ran on another Mac and it starts here with the batch, or the start does not end within
  `START_LIMIT` (60 s), the queue holds for a session, with every entry waiting.
- The hold's reason quotes the error, names the Mac and says which case was found. The session brings
  Queuekeeper and the extension folders to commits that work together on that Mac, or removes the
  entry that broke loading, and resumes. `qk queue resume` starts a worker from the current checkout,
  which is often the whole repair.
- A lane run by a Queuekeeper older than this still exits 1 there, and is read as a failure.

**A run with no verdict blames nobody.** Exit 3 (unmeasured), 69 (a worker that lost Local Network
access), 75 (no admission) and 78 (a misconfigured host) say nothing about the commits. The worker
runs the lanes again, and holds the queue, with every entry waiting, once three runs in a row have
given no verdict ([EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946): a single retry held the
queue too often). It also holds for a push refused for any reason but a race.

- A project sets another bound on `main` as `"queue": {"noVerdictRuns": N}`, a positive integer; 1
  holds at the first. A run with a verdict, or `qk queue resume`, starts the count again.
- **Locked screen.** A run that a locked screen left without a verdict does not count (owner's
  decision, 2026-11-08, [vocaro-engineering#293](https://github.com/vocaro/vocaro-engineering/issues/293)):
  exit 3, or a lane at its limit, whose last screen reading (its receipt's `screenAfter`, else its
  log's last `screen:` line) shows a lock. The worker logs that reading and runs the lane next as
  `qk run --unlocked-screen LANE`, only on a Mac whose screen reads unlocked. A run that then waits
  out its turn and exits 75 counts.
- **Locked-screen cap.** So that a lane whose screen locks during every run does not retry without
  end, `LOCKED_SCREEN_RUNS` (5, the owner's judgment; 2026-11-09) runs in a row that read unlocked at
  their start and locked by their end hold the queue for a session. A run that began behind the lock,
  or waited for an unlocked Mac and exited 75, does not add to this count. A verdict, another cause of
  no verdict, or `qk queue resume` resets it. The hold names the lane, the last screen reading and its
  Mac, so a session can find why the screen locks, such as a lane holding no assertion.
- **Disk space.** A run that ran out of disk space holds the queue at once, with no retry
  ([#162](https://github.com/vocaro/queuekeeper/issues/162)): `qk run` ends such a lane as unmeasured,
  exit 3, with a line beginning `queuekeeper: out of disk space:`
  ([Free disk space](disk-cleanup.md#free-disk-space)). The session frees space with
  `qk disk --delete`, or by removing what `qk disk` lists, and resumes. A lane that waited for space
  before starting and exited 75 counts toward `queue.noVerdictRuns`, and the hold says so.

**What the no-verdict hold says.** The reason says how many runs gave no verdict, on which Mac the last
ran (`on this Mac (NAME)` or `on NAME`), and why: the lane that reached its limit, the number of tests
left unmeasured with the first of them and the last screen reading (vocaro-engineering#185), the error
its receipt records, no admission, or the host's configuration.

- When the log reports no unmeasured test and the receipt no error, it quotes the project's last
  `Unmeasured:` line ([lanes](lanes.md)), at most `UNMEASURED_SHOWN` (300) characters
  ([#30](https://github.com/vocaro/queuekeeper/issues/30)). The worker's own `no verdict (exit 3), run
  N of at most M` log line ends with the same cause.
- When the last run ended unmeasured in a lane that takes one slot of admission and is not
  `"wholeHost": true`, the reason adds that other work can join the lane after it starts, and points a
  timed part that needs a quiet Mac at `qk admit --here --exclusive` from a `self` lane
  ([heavy-work admission](admission.md#heavy-work-admission),
  [#28](https://github.com/vocaro/queuekeeper/issues/28)).
- The hold is the owner's only when the last run's exit was 78, a shared host's configuration (which
  includes a Mac whose `provision` lane failed; the reason names it and that lane's log). Otherwise it
  is a session's ([A hold says whom it is for](#holds-and-closed-queues)). Nothing fails over to
  another route.

**A retry waits for the host to settle.** Each retry first waits by the test admission applies beyond
its guaranteed slots (`resources.pressure`;
[heavy-work admission](admission.md#heavy-work-admission)): a CPU busy share below `HEAVY_BUSY_LIMIT`
(90% by default, 70% where setup wrote it) and memory pressure at level 1. Reason: a retry that started
at once met the load that voided the run it repeated
([#124](https://github.com/vocaro/vocaro-engineering/issues/124); the busy share replaced the load
average under vocaro-engineering#200).

- It looks every `SETTLE_POLL` (15 s) for up to `SETTLE_LIMIT` (an hour), then holds, with every entry
  waiting. The hold and the log name the reason last read and whose limit it is and where it is set
  (vocaro/queuekeeper#29). A stop request (SIGTERM, SIGINT or SIGHUP) ends the wait at its next look.
- **Which Mac it reads.** With another Mac configured, the Mac the retry could use
  (`remote.retry_macs`, as `qk run`'s offer does), or both when either could
  ([#253](https://github.com/vocaro/vocaro-engineering/issues/253)); without a choice, the Mac the
  lane ran on, from its dispatch ([#140](https://github.com/vocaro/vocaro-engineering/issues/140)).
  Another Mac is asked over SSH; one that cannot be reached gives this Mac's reading.
- A lane that fails blames its commits at once, with no wait. The pre-push hook's no verdict waits the
  same way, reading this Mac.

## Holds and closed queues

The user guide's [Holds](../user_guide.md#holds) gives the outline.

**A hold says whom it is for, and a session resumes it.** Every hold's reason begins with its kind:

- `for the owner, since nobody else can fix it`: a shared host's configuration (exit 78 past the
  bound), a push the remote refused, a key or token a remote refused.
- `for a session, not the owner`: everything else, such as a host that stayed busy or tests left
  unmeasured past the bound, a screen that locked `LOCKED_SCREEN_RUNS` times, the pre-push hook's
  refusal or no verdict, a path where the worker's checkout goes, a `main` whose `queuekeeper.json`
  the toolkit at `origin/main` refuses (naming that toolkit commit), a remote out of reach past its
  tries, and anything unexpected.

The reason ends with who runs `qk queue resume`. The session that has dealt with the cause, or any
session that finds it gone, resumes itself, says so in its report, and never asks the owner. The owner
is asked only for a hold that says it is his, and once he has fixed its cause any session resumes
(owner's decision, 2026-11-04, [#173](https://github.com/vocaro/vocaro-engineering/issues/173): "this
should never need my approval"). A hold whose cause a session cannot find is still a session's: say
what was looked at and what remains. `qk queue status`, `qk land --wait` and `qk land` print the
reason with its kind.

**A closed queue takes no new entry and finishes what it has.** The procedure is in
[Closing a queue for maintenance](../user_guide.md#closing-a-queue-for-maintenance): `qk queue close
--why TEXT`, wait for `qk queue status --idle` to exit 0, work, `qk queue reopen`
([#32](https://github.com/vocaro/queuekeeper/issues/32): an idle reading alone does not stop the next
entry). Beyond that guide:

- The closure is `queuekeeper-queue/closed.json`, with why, who and when. `qk land` and
  `qk queue add` refuse before the conflict check and again under the lock that saves an entry, which
  `close` takes too, so an entry is either saved before `close` returns (and `--idle` counts it) or
  refused.
- Every new entry is refused: `--front`, `--no-gate`, and a branch's newer commits, whose earlier
  entry stays and finishes. A commit the queue already holds is no new entry, so `qk land` of it
  follows that entry, and `--front` or `--no-gate` still move it.
- The worker does not read the closure; every kind of round goes on.
- A second `close` changes nothing and exits 1, naming the first. A `closed.json` that cannot be read
  closes the queue too.

## Reaching the remote and pushing

**A remote out of reach is tried again.** A fetch or push that never reached the remote, or whose
connection ended (a name that did not resolve, a connection refused, timed out, reset or dropped, over
HTTPS or SSH), says nothing of the commits.

- The worker tries again after 10, 30, 90 and 270 seconds (`BACKOFF`). When the fifth try fails, it
  runs the round again after 1, 5 and 15 minutes (`CLEARING_WAITS`), keeping every entry and a
  batch's pass, and holds for a session only when the last round's fifth try fails too, about an hour
  in ([#173](https://github.com/vocaro/vocaro-engineering/issues/173),
  [#113](https://github.com/vocaro/vocaro-engineering/issues/113)). A round that goes through starts
  the tries again. `qk land`'s own fetch retries with the same backoff.
- Git's message decides. One that says the remote answered and refused (an authentication failure, a
  4xx status, an SSH key or host key refused, a missing repository or a rejected push) holds at once,
  for the owner, though SSH ends a refused key with the same `Could not read from remote repository`
  as a dropped connection. Any other error holds at once, for a session.

**A push refused for its credentials is tried again.** A push refused for its key or token
(`Permission denied (publickey)`, `Authentication failed`, `Invalid username or token`), when the
fetch just before it went through from the same URL, is tried again after each of `BACKOFF`. Reason:
GitHub once refused the owner's key to push for over an hour while it still fetched
([#151](https://github.com/vocaro/vocaro-engineering/issues/151)).

- On GitHub the worker then reads [its status](https://www.githubstatus.com) for Git Operations and,
  while that is not operational, tries every minute for up to `SETTLE_LIMIT` (an hour), since a hold
  costs a gate.
- It holds, giving GitHub's status, on a refusal while Git Operations is operational, past the hour,
  when the status cannot be read, or on another host.
- A push URL other than the fetch's, a refused fetch, and any other refusal hold at once.

**A pre-push hook runs ahead of the push.** `git push` connects before it runs the hook, so a hook's
long wait held an SSH connection open until GitHub dropped it. After a gate passes, the worker runs
the project's hook itself with `git hook run`, as Git would for a push to `main`, logging to
`queuekeeper-queue/logs`, then pushes with `--no-verify`.

- A dropped connection or a race that leaves the commit unchanged pushes again without rerunning the
  hook; a carried-over commit is a new commit and runs it again.
- A hook's exit 3, 69, 75 or 78 is no verdict: it is tried once more, once the host has settled, and
  then holds. Git passes the hook's status through, so a hook that waits for admission should exit
  with the status it got rather than 1.
- Any other refusal holds at once, for a session, since the gate passed and the hook disagrees. It
  blames no commit.

**A hook that repeats the queue's lanes may be covered by them.** A project opts in on `main` (a
branch cannot opt itself in), naming each lane that covers the hook and what of the hook it runs:

```json
"queue": {"lanes": ["gate", "app-gate"],
          "hookCoveredBy": {"gate": "check-all.sh, the hook's first step", "app-gate": "check-app.sh, its second"}}
```

The lanes must be ones the queue runs. The worker then skips the hook and logs that it did. Keep the
opt-in only while it is true: a hook that checks something no lane does stays a hook. A person's own
`git push` still runs the hook.

**Queuekeeper supplies a hook that runs the lanes.** A project whose hook would only repeat its lanes
can make the hook one line:

```sh
#!/bin/sh
exec python3 scripts/qk pre-push "$@"
```

`qk pre-push` reads what Git gives a hook and finds the paths the push changes:

- only paths no code lane reads (by `queue.unread`): the docs lanes;
- other paths: `queue.lanes`, or `gate`, after the docs lanes when documents changed too;
- no path at all, as when a merge has the remote's own tree: nothing, and it says so
  ([#165](https://github.com/vocaro/queuekeeper/issues/165));
- paths Git cannot list: `queue.lanes`, or `gate`.

The lanes run with `qk run` in the checkout being pushed from, admitted and placed as always.

| Hook's exit | Meaning |
|---|---|
| 0 | Every lane passed, or the push deletes a branch or changes no path |
| 1 | A lane failed |
| 3 | A lane was unmeasured: nothing ran, which is not a pass, so the push is refused |

`QUEUEKEEPER_PREPUSH=skip git push` runs nothing and says nothing was verified. Under the queue's
worker the hook runs nothing, since the worker has just run the lanes. Such a project does not need
`queue.hookCoveredBy`.

**`qk doctor` keeps `core.hooksPath` right.** A project that tracks its hooks points `core.hooksPath`
at their folder, relative to the checkout, so each worktree runs its own branch's copy. Three states
break that, and Git reports none of them:

| State | What happens | What `doctor` does |
|---|---|---|
| The repository's value is an absolute path into one checkout | Every worktree runs that checkout's hooks, not its own branch's | Makes it relative |
| A linked worktree has a value of its own, which hides the repository's. Claude Code writes one, aimed at the main checkout, in each worktree it creates | A push from that worktree runs the main checkout's hooks | Clears it when it names the same folder, and warns when it names another |
| The path names a folder that does not exist, or a hook is not executable | Git runs no hook, prints nothing, and the push succeeds | Warns |

A fresh clone has no `core.hooksPath`, so it runs no hook. A project names its hooks folder in
`queuekeeper.json` so that `doctor` sets it where it is unset, and warns where it names another folder
or the folder is missing (vocaro/queuekeeper#131, 2026-11-13):

```json
"gitHooks": "tools/repository/hooks"
```

Without the key, a tracked `pre-push` in a hooks folder that runs `qk pre-push` is taken for the
project's own: `doctor` warns that the path is unset and changes nothing. Any other project with no
`core.hooksPath`, or one outside the repository such as a person's global hooks, is left alone.

**`qk doctor --hooks` is the quick form for a session-start hook.** It checks and repairs
`core.hooksPath` alone, in about a second, printing one line per finding (`repaired` or `warning:`) or
one line beginning `fine:`. It exits 0 when nothing was wrong or everything was repaired, and 1 when a
warning is left for the person. A Claude Code `SessionStart` hook runs it as it is:

```json
{"type": "command", "command": "python3 scripts/qk doctor --hooks", "timeout": 15}
```

## Landing ahead of the queue

Two options of `qk land`, and of `qk queue add`, are for emergencies only, never routine. They work
in every repository, with no setting (owner's decision, 2026-11-04,
[#163](https://github.com/vocaro/vocaro-engineering/issues/163)), and do what a hand push did, with the
tool keeping the record. The user guide's [Emergencies](../user_guide.md#emergencies) gives the
outline.

```sh
python3 scripts/qk land --front --why "the toolkit fix every queued change needs" --wait
python3 scripts/qk land --no-gate --why "an unrelated flaky test fails the gate" --wait
```

- **`--front`** tests the entry next, as a batch of its own. A running gate finishes first, as does a
  passed batch waiting for its push; a bisect goes on where it was. `--why` is optional and shown by
  the entry and `qk queue status`. The entries already queued are then tested with the fix: they
  rebase onto the new `main`, and other projects' workers move to the new toolkit each round.
- **`--no-gate`** requires `--why TEXT`. It lands the entry alone, in its turn: rebased onto
  `origin/main`, with only the preflight lanes and the pre-push hook (unless `queue.hookCoveredBy`
  covers it). The reason is recorded in the entry's `ungated`, the worker's log, the landed tip's
  `Landed-without-gate:` trailer and `qk queue status`. It is no gate of the pushed commit, so
  `eng release` will not take it, and `qk queue status` lists it as `ungated on main` until a later
  code gate on a base that holds it passes. A session that uses it tells the owner it did and why.
- **Both together** land the entry next, ahead of any other `--front` entry. While a gate's lane
  still waits for admission, an unlocked screen or a Mac ([A deep queue](#state-and-recovery)), the
  worker stops it within `REMOVAL_POLL` (5 s), lands the entry, and gates that round again on top of
  it ([#26](https://github.com/vocaro/queuekeeper/issues/26)). This holds for every gate: a batch, a
  bisect's halves, a `--front` gate and a stacked one. A gate that has started finishes first, and
  `qk queue status` says `waits for the running gate of ID, which has run Nm`.
- `qk land --front` again on a queued commit moves that entry, and `--front --no-gate` again on an
  entry testing in a waiting round takes it out of that round. Any other option given again to a
  testing entry changes nothing, and `qk land` says how to land it alone without the gate.

**Pushing by hand is the exception.** A session pushes to `main` by hand only when `qk land` cannot be
used or must be bypassed, for a reason it can state, such as a broken queue, or with the owner's
approval. A fix that cannot wait for the gate uses `qk land --front --no-gate --why TEXT` instead. A
record or a document lands by the queue like anything else. A hand push follows
[push only onto the `main` you tested](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/maintenance.md#concurrent-git-integration)
and is reported to the owner with its reason. No check enforces this.

## Documentation and carry-over

The user guide's [Documentation changes](../user_guide.md#documentation-changes) gives the outline.

**Paths no code lane reads.** One setting on `main`, `queue.unread`, answers two questions: may a
batch skip the gate (every path it changes is unread), and does a passed result stand when `main`
moves (every path `main` gained is unread and untouched by the batch). One list answers both, so the
two can never disagree.

```json
"unread": {"paths": {"doc/guide/": "read by no code lane; qk docs check reads it",
                     "records/": "landing records; no lane reads them"},
           "exclude": {"doc/guide/input.md": "a code test reads this fixture"},
           "lanes": ["docs"]}
```

Every key is optional:

- `paths` adds to the Markdown defaults, each a file or a directory ending in `/`, with why no code
  lane reads it. A directory takes every file in it, not only Markdown, so an image a document shows
  there needs an `exclude` entry or another place.
- `exclude` names paths that code does read, and wins over both.
- `lanes`, by default `["docs"]`, is what an unread batch runs in place of the gate, never `gate`.
- `defaults: false` drops the Markdown defaults. `"unread": false` gates every batch and carries
  nothing over.

List a path only when no lane reads it, and check first: studywright's gate compiles sources under
`measurements/audits`. A path left out costs a gate run; a path wrongly listed lands untested.

A path that only one or two small checks read can name those checks' lanes in place of a plain reason
(vocaro/queuekeeper#27):

```json
"paths": {"doc/release/": {"why": "only the privacy checks read it", "lanes": ["privacy-docs"]}}
```

A docs-only batch that changes such a path runs `lanes` plus every lane its paths name, recorded as
`docsNamed` with the paths and why (shown by `qk queue status` and `qk queue result`). Since those
lanes read the path, `main` gaining it never carries a result over. A batch that changes it beside
code rides the gate.

`queue.unread` replaces `queue.docsOnly` and `queue.carryOver`, which may not stand beside it. This
release reads them as `queue.unread` (`carryOver`'s paths join `docsOnly.paths`), and `qk doctor` and
`qk land` warn and print the `queue.unread` to use; the next release refuses them. `docsOnly: false`
beside a `carryOver` gates every batch and still carries those paths over, until the owner chooses.

**Main moving under a passed batch.** The queue takes a hand push as it comes. If `main` moved only by
unread paths the batch does not touch, the worker checks that the batch's patch is unchanged on the
new `main`, runs `qk docs check` when a document moved, then the project's `queue.carryOverChecks`, and
pushes. A carried-over result is no gate of the pushed commit, so `eng release` will not take it. Any
other move gates again, with no blame.

**Checks before a carry-over.** A project whose rule asks for more than the docs lane names the
commands in `queue.carryOverChecks`, run in order on the batch rebased onto the new `main`:

```json
"carryOverChecks": [
  {"command": ["python3", "tools/check_records.py", "--base", "{base}"], "paths": ["records/"]},
  {"command": ["python3", "scripts/qk", "docs", "check"]}
]
```

- `command` is an argv list; `paths`, optional, limits a costly check to moves of those paths.
- Each runs supervised, with `LANE_LIMIT` and a log under `queuekeeper-queue/logs`; heavy work inside
  one goes through `qk admit` or `qk run`.
- A check that fails, times out, cannot start or gets no admission ends the carry-over: the batch
  gates again, with no blame and no hold.
- A diff against `origin/main` sees nothing, since the batch already sits on it, so a check takes
  `{base}` (anywhere in a word), the commit the result passed on. Prove each check fails on a bad move
  before relying on it.

**A docs-only batch runs the docs lanes.** A batch whose every path is unread is docs-only. The
defaults are the root `README.md`, `CONTRIBUTING.md` and `CHANGELOG.md`, and Markdown files (`.md` or
`.markdown`) under `doc/` and `docs/`. `guides/` and `knowledge/` are not defaults
(vocaro/queuekeeper#106): a project names them in `queue.unread.paths`.

- The worker runs the preflight lanes, then the project's `docs` lane or the toolkit's implicit one
  (`python3 scripts/qk docs check`, or the toolkit's own `bin/qk` in a project with no launcher).
- Code, configuration, agent instructions, non-Markdown data, and documents under source or test
  folders keep the code gate. A batch that changes nothing runs every code lane, as does an entry
  whose own commits change both a document and code.
- A test that reads a document needs a declaration, since nothing finds it. Queuekeeper's own
  `queuekeeper.json` names `doc/user_guide.md` with the `setup-examples` lane, which runs the one code
  test that reads it ([vocaro/queuekeeper#147](https://github.com/vocaro/queuekeeper/issues/147)).
  A project whose code consumes a default documentation path names it in `queue.unread.exclude`, or
  sets `queue.unread` to `false`.
- Name any project-specific documentation validators in `queue.unread.lanes`. Only `main`'s settings
  count, so a branch cannot widen its own classification. A move counts only when both the old and
  new paths qualify.
- Several docs-only entries land as one batch. The pre-push hook is skipped for a docs-only batch,
  with or without `queue.hookCoveredBy`, so put required documentation checks in the docs lanes. No
  emergency option is needed for documentation.
- A docs-only landing is no full gate of the pushed commit: `gate` may not be a docs lane, no gate
  receipt names the commit, and `eng release` will not take it. The log and `qk queue status` record
  the docs lanes run.

`qk land` says which an entry will ride as it queues it, naming the first path in sorted order that
is not documentation (vocaro/queuekeeper#118).

**A batch of documents and code runs the docs lanes too** (vocaro/queuekeeper#122). When a batch
changes an unread path beside any other, the worker runs the docs lanes for those documents as well as
the gate: after the preflight lanes and provision, ahead of `queue.lanes` or `gate`, so a cheap failure
returns the batch before the gate starts. Reason: a gate once let an `AGENTS.md` past its docs lane's
size limit, which the next docs-only batch would have failed on.

- A docs lane that `queue.lanes` already names, alone or in a group, runs once.
- Both halves of a bisect run the docs lanes when either changes a document; a stacked gate chooses
  by its own entries' paths. `qk pre-push` does the same, docs lanes first.
- A project need not copy its docs checks into its gate. `queue.unread: false` turns this off.

**A docs-only entry never rides a code gate** (owner's rule of 2026-11-11, vocaro-engineering#394).
The worker classifies each waiting entry by the paths its own commits change and keeps docs-only
entries out of code batches:

- Queued beside code entries, the docs-only entries land first, in a round of their own; the code
  entries gate in the next round.
- While a code batch gates (a bisect's half or an entry put first among them), a waiting docs-only
  entry lands beside the gate, in a checkout of its own with the suffix `-docs`, looked for every
  `STACK_POLL`. Only entries that change none of the gating batch's paths go there. When the code
  batch passes, its result carries over the documents `main` gained, and so does a gate stacked on it.
- A docs-only entry goes ahead of an older entry only when the two change no path in common, and one
  left in line holds back every later entry that shares a path with it, so conflict detection is
  unchanged. A `--front` or `--no-gate` entry still goes first; no docs entry goes beside the gate
  while one waits.
- A docs entry that fails its docs lanes ahead of older entries may need one of them, as a link needs
  the file an older entry adds. It is not sent back: it records them as `waitsFor` and goes back in
  line behind them, then lands, or fails and is sent back, on top of them.

## State and recovery

**State and recovery.** Each repository's state is under `queuekeeper-queue` in its common Git
directory, so every worktree sees one queue.

- A worker starts with `qk land`, apart from the session, and exits when nothing waits. A queue set up
  with the [signed launcher](maintenance.md#signed-queue-launcher) also checks for waiting work at
  login and every two minutes. After a reboot, `qk queue wait` can start a worker, and
  `qk queue resume` can clear a hold and start one.
- After taking the queue lock, a worker requests a stop for each run whose dispatcher is proven gone,
  including a crashed worker's gate (doc/guides/another-mac.md, How a run reaches another Mac).
- What a dead worker left testing was never pushed, so the next worker tests it again.
- A batch whose lanes passed is recorded `passed`, with its commit and base, before its push. A hold
  the push meets keeps it so, and says so. After `qk queue resume`, the next worker pushes that commit
  without gating again, while the batch is unchanged; an entry removed or added since gates what is
  left, and a moved `main` is the carry-over's decision.
- A lane may take `LANE_LIMIT` (6 h: queuekeeper's default limit for the lane and two more for its wait
  for admission).
- The worker's checkout is locked against sweeps and keeps ignored inputs a provisioning lane made
  between rounds.
- A project with `"integration": "pull-request"` on `origin/main`, or with no gate lane on
  `origin/main` or in the commit added, is refused, and the command says what to add.
- Neither `qk land` nor the worker reads any checkout's working tree for settings: the worker reads
  them from `origin/main` in its own checkout, and `qk run` there reads each lane from the batch.

**A deep queue.** `qk queue status` says why entries wait, from `queuekeeper-queue/round.json`, the
worker's account of its round: whether it bisects, with the suspects and each half's size; each running
gate's lane, Mac and age; what a gate waits for and since when; a stacked gate; and a wait for the host
to settle.

- What a gate waits for comes from the record `qk run` keeps in `.build/queuekeeper/.waits/LANE.json`
  on any Mac (another Mac sends it back as it changes; it is cleared on admission), such as admission
  behind another project's whole-host gate, an unlocked screen, another Mac's foreground or a Mac out
  of reach. The record says only what a lane waits for, never how it ended: a stale one is ignored,
  and a lane's verdict stays its receipt's.
- A gate held up for `STUCK_SECONDS` (15 minutes) by a dead or uncertain admission entry is shown as
  `stuck:`, with `python3 scripts/qk admissions recover` to list the entries, and the log says so once
  (vocaro-engineering#315; doc/guides/process-supervision.md, Lock reclamation). The queue's doctor
  acting on it is vocaro-engineering#290.
- `--json` has the same under `round`, with each gate's `waitingSince` and any `stuck`.

For more, read `queuekeeper-queue/worker.log`, the worker's log; each gate's log in
`queuekeeper-queue/logs/`, named `ROUND-LANE.log`, which `qk queue status` names; and, for a gate
another Mac ran, its full output in the queue checkout's
`.build/queuekeeper/remote/.dispatches/LANE/*/run.log`, which the gate's log also copies once the lane
ends.

**A queue that is stuck: `qk queue doctor`.** `qk queue status` says what the queue is doing;
`qk queue doctor` says whether that is testing or being stuck, and for each finding what it means and
what heals it ([#290](https://github.com/vocaro/vocaro-engineering/issues/290)). It reads the entries,
the hold, whether a worker runs, and the logs of up to the newest twelve `qk unittest` gates back to
the last verdict, skipping running gates and speculative stacks no Mac admitted. Its signals:

- **refused gates**: gates that `qk unittest` refused with `nothing ran`, how many in a row with the
  same refusal, their logs, and the waiting entry whose tree alone gives that refusal, named as the
  cause only when exactly one does.
- **refused before running**: a waiting entry whose tree `qk unittest` refuses, queued before that
  check or by an older toolkit.
- **no verdict**: two or more gates in a row since the last verdict with no `qk unittest` summary or
  with tests unmeasured ([#253](https://github.com/vocaro/vocaro-engineering/issues/253),
  [#268](https://github.com/vocaro/vocaro-engineering/issues/268),
  [#282](https://github.com/vocaro/vocaro-engineering/issues/282)).
- **came back**: a waiting entry holding the very change, by stable patch ID, that an earlier entry
  from the same branch or with the same subject held when it failed or was taken out.
- **no worker**: entries waiting, the queue not held, and no worker running
  ([#287](https://github.com/vocaro/vocaro-engineering/issues/287)); **held**: the hold's reason.
- **held back on a Mac with spare CPU**: a running gate that has waited `IDLE_WAIT_SECONDS` (15
  minutes) or more for admission on a Mac under its `HEAVY_BUSY_LIMIT`. It names what the gate waits
  behind and each holder of admission there, with its command, PID, age, CPU use and the repository,
  branch, worktree and session it comes from, so its owner can be found
  ([vocaro/queuekeeper#89](https://github.com/vocaro/queuekeeper/issues/89)). Holders or CPU use that
  cannot be read are `unknown`, never guessed light
  ([vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)). A busy Mac's wait is no
  finding; one whose busy share cannot be read gets a `note:`.

It exits 0 and says `healthy` when it finds none of these, and 1 otherwise; `--json` gives the same,
with a held-back gate's Mac, busy share, limit and holders as `qk admissions now --json` gives them. A
shorter wait, or one on a busy Mac, gets a `note:` pointing to `qk admissions now`
([admission.md, Who holds and who waits](admission.md#who-holds-and-who-waits)), as `qk doctor` does
for a lane of its checkout.

The doctor changes nothing. The worker acts on a refused gate itself
([A gate `qk unittest` refused](#flakes-blame-and-bisecting)); for the other findings a session acts,
as each `next:` line says. Restarting a dead worker (#287) and moving a no-verdict run to another Mac
are not the doctor's yet.

Use [`qk queue placement`](another-mac.md#check-gate-placement-without-running-a-gate) to see whether
each configured build Mac could admit a gate lane now, even when the queue is empty.

## A project's first landing

The commit that adds a project's `queuekeeper.json` lands through the queue like any other, with
`qk land --wait`. The queue reads settings from `origin/main`, so that no branch chooses its own gate,
but `main` has none before this commit. Until it has a `queuekeeper.json` that names lanes, the queue
reads the entry's when it is queued, and the batch's in a round, for that round alone, and says so:
`qk land` prints `origin/main has no queuekeeper.json yet, so the queue reads COMMIT's for this landing
alone`, and the round's log `main at COMMIT has no queuekeeper.json yet: this round reads the batch's`.
The round runs the lanes it names, `provision` included.

This trusts the entry with its own gate only where `main` has no gate for it to loosen. Four settings
loosen what the queue checks, and only `main` gives them, even then:

- `queue.hookCoveredBy`, which skips the pre-push hook;
- `queue.unread`, which skips the gate and lets a result carry over, with the older `queue.docsOnly`
  and `queue.carryOver`;
- `queue.spelling: false`, which turns off the toolkit's spelling check (owner's decision,
  2026-11-13, [#10](https://github.com/vocaro/queuekeeper/issues/10));
- `queue.preflight`, which can name fewer checks or none
  ([#69](https://github.com/vocaro/queuekeeper/issues/69)).

A project that wants one of them off lands its configuration past the check once. After the commit
lands, every later entry is gated by `main`'s settings, `queue.noVerdictRuns` and the lanes included,
and a commit with no `queuekeeper.json` while `main` has none is refused before it is queued. Before
this, the owner's eight projects went in by hand on 2026-11-12, skipping the `provision` lane a round
runs ([#10](https://github.com/vocaro/queuekeeper/issues/10)).
