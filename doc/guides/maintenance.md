# Maintenance

These mechanisms and lessons apply when the stated capability is present. Original
measurements establish their recorded platform and workload; they do not establish
universal behavior on later operating systems or in a different application.

## The merge queue

Six of the owner's repositories landed 8 to 14 times a day in the two weeks to 2026-10-31, so a
45-minute gate that restarts whenever `main` moves is usually beaten by the next landing: in the
busiest, 82% of landings were followed by another within 45 minutes. The queue makes one process
per repository the route to `main`, as GitHub's merge queue does, on the owner's Macs:

```sh
python3 scripts/qk land --wait                 # HEAD, as a background command: 0 landed, 1 came back, 75 held or still waiting
python3 scripts/qk land BRANCH                 # returns at once, and the commit lands by itself
python3 scripts/qk land --repo EPUBLib BRANCH  # from any project: land a fix in another repository
python3 scripts/qk queue status
```

The budget and release-limit checks after `qk land` are advisory. They read the fields they
need even when main uses newer lane settings. If the advisory inputs cannot be read, the caller
prints a warning and preserves the queue outcome and exit status (vocaro-engineering#355).

**A gated push keeps the app.** For a project whose queuekeeper.json has `app`, the worker starts
`qk app keep --after-landing` for the pushed commit in the background once the push lands, and the
round goes on without waiting. That build is what `qk app run` opens ([kept app builds](app-builds.md)).

**A session does not run the full gate.** It runs the smallest relevant checks, commits and adds
the commit. Adding is the decision to land: the owner approves that once, and no later step asks.
What is added is the range the ref has and `origin/main` lacks, held by a ref of its own, so the
session may end and its branch may move or go. Refreshing a branch by merging `main`, by the
app's sync or `git merge origin/main`, is fine: when each merge in the range brings in only commits
`origin/main` holds, with the tree a clean merge makes, `qk land` replays the branch's own commits
onto `origin/main` in a scratch worktree, queues that, says which merge it dropped, and leaves the
branch as it is, whose work the landing check then finds as equal patches. Any other merge, or a
replay that conflicts, is refused (vocaro-engineering#217). Adding a branch again replaces what it
added before.

**A fix in another repository lands there the same way.** Commit it in a worktree of that
repository, then `qk land --repo NAME BRANCH --wait`, from wherever the session is. It joins that
repository's queue beside its own sessions' commits and so stops none of their work. A commit must
be named, because HEAD there is another session's. `--wait` prints `landed: COMMIT` in full: the
entry's own last commit on `main`, not another entry's at the batch's tip, and its status line
above says `-> COMMIT (N commits), in batch tip TIP` when the entry has several commits or another
entry's commit tops the batch. Cite that commit. Move the dependent project's pin to it in a
commit of its own, and land that in the dependent's queue.

**A round.** The worker fetches, takes everything waiting, oldest first, cherry-picks each entry
onto `origin/main` in its own checkout, `~/Development/queuekeeper-workers/NAME/NAME`, and runs the project's queue lanes through
`qk run`: its preflight lanes, `queue.preflight` ([Preflight lanes](../user_guide.md#preflight-lanes)), and its
`provision` lane when it has one, and then its `gate` lane, or
`queue.lanes` in queuekeeper.json for a project that needs others, read from `main`, or from the
batch until `main` has any, so that the commit naming them can land
([A project's first landing](#a-projects-first-landing)). Until a project gives `queue.preflight`,
its preflight lane is its `spelling` lane, and a project with no `spelling`
lane gets the toolkit's, which a later release removes
([#69](https://github.com/vocaro/queuekeeper/issues/69)): the worker runs `python3 scripts/qk spelling` as a lane, unadmitted
in its own checkout (*The short checks run without admission*, below), before anything else, so that a British spelling fails in
about a minute instead of after a full gate and a bisect, in every project without each declaring the
same lane. A project needs no launcher for it, nor for the round's `qk run`: where the checkout has
no `scripts/qk`, the worker runs the check and `qk run` with the toolkit's own `bin/qk`, and a
`qk run spelling`, which a check that did not end in time goes through, runs the
check with the toolkit that runs it, on whichever Mac that is, where another Mac's is the toolkit
sent with the lane. On 2026-11-13 apple-engineering's first landing failed in this lane, as `python3`
could not open the `scripts/qk` that getting started never asked it for
([#108](https://github.com/vocaro/queuekeeper/issues/108)). A run that gets no admission or ends unmeasured is a no verdict, as any lane's is, and a
finding rejects the batch. A project's own `spelling` lane runs in its place, and
`"queue": {"spelling": false}` on `main` keeps the queue from running the toolkit's for a project
that cannot pass it yet; the round's log says when the toolkit's ran. The
lanes therefore go where every lane goes, to whichever Mac has spare capacity for them, another Mac
first ([which Mac runs a lane](shared-host.md#which-mac-runs-a-lane)), and the round's log says where
each ran and why; one gate serves the whole batch. Each lane is placed by itself, so the `provision`
lane may run on one Mac and the gate on the other: `qk run` therefore runs the project's `provision`
lane first in the checkout of whichever Mac runs a lane, the worker's own included
([vocaro/queuekeeper#19](https://github.com/vocaro/queuekeeper/issues/19)). On a pass it pushes
exactly the commits it tested, as ordinary commits with their own messages and closing lines, so
Closeout finds each branch landed by equal patches. The gate receipt is of the pushed commit, which
`eng release` accepts, unless the result was carried over. Each landed entry records its gate's wall
time, and apart from it the gate's own work from its admission to its end and its wait for admission
([#112](https://github.com/vocaro/queuekeeper/issues/112)), with the CPU busy share of the Mac the gate ran on, read every
minute after its admission, so that the gate's budget judges its own work and its median can leave out runs made on a
loaded host ([the gate's time budget](lanes.md#the-gates-time-budget), [#235](https://github.com/vocaro/vocaro-engineering/issues/235)).

**Lines added to a changelog section need no author.** Branches that each add a line to a section of
`CHANGELOG.md`, in any directory and any case, conflict in turn, since each line sits where the one
before it just went. The worker settles such a conflict by keeping both, the new lines first, so the
entry being landed goes above those ahead of it, and gates the result as usual. It does so only when
every conflict in the commit is in a changelog and each is lines both sides only added at one place,
as whole lines, with no heading and no blank line among them, and with none of the commit's lines
already in the file, since keeping both would double it. The place is directly below a Markdown `#`
heading or, when each side's lines start a list item, directly below one, as when two branches each
put their line second ([#130](https://github.com/vocaro/vocaro-engineering/issues/130)). For an entry
of several commits the worker merges the entry's whole change to the changelog, from its first
commit's parent, onto what is ahead of it, so a later commit that rewords the line an earlier one
added is still that line added. Anything else comes back, naming each file in conflict: a change to a
line ahead of it, lines below a paragraph or inside a wrapped entry, or a conflict in another file
above all. Each settled commit is listed under `kept` in the entry's record, which `qk queue status`
and `qk queue wait` show. The commit keeps its message and author. Its patch is then not the
branch's, so `landed.sh` proves the branch landed another way, `resolved`: each branch commit has a
commit on main with its subject and author that adds and removes exactly its lines, file for file
and hunk by hunk, and differs only in the lines around them. It compares each commit with the one
it landed as, so a line later work on main rewrote still counts. Closeout and its sweep then delete
the branch like any other landed one (the owner's decision of 2026-11-04,
[#164](https://github.com/vocaro/vocaro-engineering/issues/164)). A resolution that dropped or
changed one of the branch's lines proves nothing, and that branch stays for its author.

**Which toolkit a gate runs.** Each round, a project's worker moves
`~/Development/queuekeeper-workers/NAME/vocaro-engineering`, a locked worktree of the toolkit with no session's
edits, to the toolkit's `origin/main`, and runs the lanes, the pre-push hook and the carry-over checks
with `QUEUEKEEPER_ROOT` naming it (owner's decision, 2026-11-01). A landed toolkit commit
therefore reaches the next round of every project's gate, and a shared toolkit checkout that lags or
holds a session's changes reaches none of them. Each worker has its own copy, so moving it changes no
other project's running gate. The worker follows its copy: once the copy is at `origin/main`, a worker
whose own code came from another commit starts itself again from the copy, with the same arguments and
environment and the bisect it had reached, before the round touches any entry, and its log says
`moving to the toolkit at COMMIT, origin/main, from COMMIT, which this worker was started from`. So what
judges `main`, its `queuekeeper.json` above all, is the toolkit the lanes run, not the older code of the
checkout the worker was started from. On 2026-11-04 EPUBLib's worker, started before
[#168](https://github.com/vocaro/vocaro-engineering/issues/168) added the lane key `wholeHost`, landed
a commit whose gate lane set it and then held the queue on `Invalid lane gate`, as its own code refused
the key its copy had accepted, until a person resumed it ([#173](https://github.com/vocaro/vocaro-engineering/issues/173)). The toolkit's own queue's lanes test its
batch instead, but its worker moves the same way, to a copy of its own that no lane reads: a running
worker keeps the modules it loaded, so one started from the shared checkout, or a session's worktree,
would judge the next round's `main` with that code even after the shared checkout followed a landing,
and a toolkit commit that added a key and set it in the toolkit's own `queuekeeper.json` held the queue
once ([#177](https://github.com/vocaro/vocaro-engineering/issues/177)). Since every toolkit landing
moves `origin/main`, that worker starts itself again in the round after each one. A
`QUEUEKEEPER_ROOT` set where the worker started stands and does not move it.

A worker does not move to a toolkit that could leave the queue with no worker to land its fix. Before a
move it imports the worker's code from that commit's files in a process of its own, and the queue's
directory keeps the commit a worker moved to, `moved`, until the worker started there finishes a round.
A commit that does not import, or one a moved worker died or held in before finishing a round, is not
moved to: the worker goes on with its own code, a project's lanes with the toolkit it was started from,
and its log says `staying on the toolkit at COMMIT, which this worker was started from, rather than
moving to COMMIT, origin/main:` and why. So a broken toolkit pushed to `main` by hand is fixed through
the toolkit's queue as any commit is, and the next commit on `main` is moved to again. A copy that cannot be fetched is a remote out of reach, tried again as any is, and one that
cannot be moved holds the queue for a session; neither blames a commit. Anything else still
reads the shared checkout: a session's own `queuekeeper`. So a project commit that needs a new toolkit option lands after the toolkit's commit, and runs
by hand only once that checkout has it.

**A worker whose session ended exits.** macOS denies a worker the local network once the Claude session that
started it ends, so it could no longer reach another Mac. Before each round, while another Mac is configured, the
worker checks for this and, if so, exits, holding nothing, with its entries back in the queue. A gate that finds it
during a round, by the exit 69 of a lane's `qk run`, which then ran nothing on this Mac, does the same. Its log then
says `exits, holding nothing, since this worker cannot reach another Mac`. The next `qk land`, `qk queue wait` or
`qk queue resume` starts another worker (doc/guides/shared-host.md, Local Network access;
vocaro-engineering#282, #300).

**The shared checkout follows each toolkit landing.** After each push, the toolkit's own worker
fast-forwards its main checkout, `~/Development/queuekeeper`, to `origin/main`, without hooks,
but only when that checkout is on `main`, clean (no changed or untracked path), holds no commit of its
own, is behind, and no process started from it still runs: one whose working directory, an open file
or an argument is below the checkout, in its tree, outside the session worktrees inside it and Git's
own directory, where an argument relative to its working directory counts when it names a file there,
or a folder by a path with a slash
(owner's decision, 2026-11-02, [vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)).
A commit made in that checkout and landed through the queue is not one of its own: the queue lands it
under another SHA, so `main` there is beside `origin/main` rather than behind it, and the worker, finding
each such commit's change on `origin/main` by `git cherry`, moves `main` there with `git reset --keep`
and says how many it moved past. One commit whose change `origin/main` lacks keeps the checkout as it is
(2026-11-06: a session's commits there held it six commits behind).
The checkout itself is no such use: every Claude session in a worktree inside it passes it as
`--project-config-root`, and the session's MCP servers and language servers start there, while a run
from it, such as `python3 scripts/qk`, names a file below it
([vocaro-engineering#182](https://github.com/vocaro/vocaro-engineering/issues/182)).
Nor is a program other than Python whose only tie is its working directory, such as a session's shell
left in `src/python` (only Python loads code from there unnamed), or a signed queue
launcher, which names the checkout's `src/python` in its arguments but only waits on the worker it
started, which counts by its own (`Worker.stays`,
[vocaro-engineering#384](https://github.com/vocaro/vocaro-engineering/issues/384)).
Nor is a process that restarts itself once the checkout moves. A long-running one, such as `qk serve`, registers
under the checkout's Git directory, in `queuekeeper-restartable/PID.json` with when it started; once the
checkout's HEAD moves it replaces itself, by exec, with the same command from the new code, and an open
dashboard page reloads when the commit it serves changes. A registration whose process has ended, or whose PID
another process now has, is removed. Nor is a process whose working directory or argument lies under
`.claude/worktrees/` in the checkout, whether or not Git still lists that worktree, such as a shell left naming
one that has been removed (owner's decision, 2026-11-17: Queuekeeper's scripts and code are free to restart as
needed; [vocaro/queuekeeper#159](https://github.com/vocaro/queuekeeper/issues/159)).
A running `queuekeeper` keeps the modules it loaded at its start but reads six others when first used, and a
lane that starts another `queuekeeper` reads the tree as it is then, so a run spanning the move could mix two
toolkits. Otherwise the worker leaves the checkout as it is and its log says why, naming up to five
of the processes and what of each it found, such as
`left ~/Development/queuekeeper as it is: it is on BRANCH, not main`; nothing of this holds
the queue, and the next landing tries again. A checkout left only for such a run, or for a census that
failed, the worker also tries again once nothing waits, every 30 seconds until it moves, so it moves as
soon as the last such run ends (owner's decision, 2026-11-11, vocaro-engineering#384); an entry that
arrives or a hold ends the tries, and the next landing's follow starts them again. While it tries,
`qk queue status` shows the worker running and waiting for the shared checkout, with a `held by` line for
each of up to five processes that hold it, in words, its PID and how long it has run, such as `the queue worker
for mpm`, `a lane's xcodebuild through the Apple plugin's wrapper` or `` a `qk unittest` command ``, and
`qk queue doctor` gives the same as a note; its log names what holds it when that begins or changes
([vocaro/queuekeeper#159](https://github.com/vocaro/queuekeeper/issues/159)); one left for any other reason is a person's to settle. A worker whose code came from that checkout, the
toolkit's own among them, is such a process until its first round after a toolkit landing, when it
starts itself again from its copy, above. A process that loaded the toolkit from it without keeping a path there,
as a project's launcher does, cannot be seen, and a process that starts between the check and the
fast-forward is not either. A session never stashes, resets or edits that checkout by hand to make it
follow.

**Where the queue's checkouts are.** Each queue's pair is in one folder beside the main checkouts and
outside every repository, laid out as another Mac lays out a run's (`checkouts/NAME/N/...`):

```
~/Development/queuekeeper-workers/NAME/NAME                 the worker's checkout of the project
~/Development/queuekeeper-workers/NAME/vocaro-engineering   its copy of the toolkit
```

So a project file that names the toolkit by a path relative to the checkout, as Pagepour's and
studywright's `../vocaro-engineering/src/swift/EngineeringTestSupport` do, finds the queue's own copy,
on this Mac as on another Mac, with no change to the project. The toolkit's own queue is
`queuekeeper-workers/vocaro-engineering/vocaro-engineering`, with the copy its worker runs from beside it,
`queuekeeper-workers/vocaro-engineering/vocaro-engineering-toolkit`. A folder inside the
toolkit's checkout was set aside: `git clean -x`, a reclone and the toolkit's own tree-walking checks
would all reach it (owner's decision, 2026-11-02,
[vocaro-engineering#104](https://github.com/vocaro/vocaro-engineering/issues/104)).

The checkouts an earlier worker kept beside the main checkout, `../NAME-queue` and
`../NAME-queue-toolkit`, move by themselves: the first round of a worker running a toolkit that has
this layout moves each with `git worktree move`, so Git knows it at its new place, it stays locked, and
the ignored inputs a provisioning lane made come along. Package state that names the old path does not:
the worker removes each `.build`'s `workspace-state.json`, `artifacts`, `checkouts` and `repositories`,
and each Xcode `SourcePackages`, whose `workspace-state.json` names it, so that SwiftPM and Xcode resolve
the packages again; build products stay ([#114](https://github.com/vocaro/vocaro-engineering/issues/114)).
That worker holds the queue, so no gate of its own runs there. A process that still works in one, as a lane an earlier worker left running may, keeps
it where it is, as does a process census that fails or a move Git refuses: the worker then makes the
checkout afresh at the new place, locks the old one as `retired by the merge queue: replaced by PATH`,
and says so in its log, and `qk disk` lists it for the owner's removal once nothing uses it.

**A conflict is found when the entry is queued.** `qk land` and `qk queue add` make the
integration a round would make, before they queue anything: onto `origin/main`, after every entry
ahead of it that is waiting, in its gate or passed, in the order rounds land them (a batch that
passed, one in its gate, those `--front` put first, then the rest, each oldest first), with the
worker's own code, its changelog resolution among it, in a scratch worktree outside every checkout
that goes when the check ends. An `--front` entry is tested alone, so it is checked against
`origin/main` only. An entry that does not apply is refused at once, with nothing queued and exit
1, as for an entry that came back: the refusal names the files in conflict, every entry ahead it was
checked against, in order, the ones among them that change those files, and any entry ahead it left
out because a round would send that one back too. A changelog conflict the worker settles is no
conflict, and the check says the round would settle it. Before
[#178](https://github.com/vocaro/vocaro-engineering/issues/178) only the round found a conflict, so an
entry queued behind a running gate learned of it 15 to 40 minutes later, more on EPUBLib. The check
is only a hint: `main` and the entries ahead can change before the round, as an entry ahead comes back
or a hand push lands, so the round integrates again and still sends back what the check could not
see, below, and an entry ahead that comes back may let a refused entry apply, which `qk land` again
checks. The refusal also names the commit's own lines in conflict, as `PATH:FIRST-LAST` of its
version of each file, from the trial's conflict blocks, so that its author can resolve them before
the entries ahead land without replaying the queue by hand, and, when entries ahead change the files
in conflict, the command that waits for them, `qk queue wait ID ID ...`, to run as a background
command. That wait ends once none of them is queued, in its gate or passed, so an entry a `--front`
entry sends back to queued keeps it going, and says what is worth doing: exit 0 when each landed, so
rebase onto `origin/main` and `qk land` again; 1 when one came back, so the commit may apply as it
is and `qk land` again checks; 75 while the queue is held or at its `--limit`
([#304](https://github.com/vocaro/vocaro-engineering/issues/304)). `qk land --wait` does this
itself when the conflict is with entries ahead alone, not with `origin/main`: it says it waits for
them, waits, and checks again against the whole queue, so an entry ahead that comes back lets the
commit be queued and land with no second command, an entry queued meanwhile that touches the same
lines is waited for in turn, and once they have landed a commit that still conflicts, now with
`origin/main`, is refused, saying to rebase, exit 1. A conflict with `origin/main`, alone or beside
entries ahead, is refused at once, as before, since no wait ends it. On 2026-11-07 two sessions
hand-wrote loops over `qk queue status` instead, one of which a `--front` preemption ended early, and
one landing took 4.5 hours of rounds of refusal, replay and wait
([#302](https://github.com/vocaro/vocaro-engineering/issues/302)). A check that cannot be made, such as a
worktree Git cannot add, queues the entry, says so, and leaves any conflict to the round. It costs a worktree checkout and the cherry-picks, measured on
2026-11-04 on this Mac at a one-minute load of 28 to 45 with six one-commit entries ahead: about
0.6 s with none ahead and 1.6 s with six on the toolkit's tree of 203 files, and 4.5 s and 6 s on a
tree of 10,000 files, where each cherry-pick adds about 0.3 s. Whole `qk land` runs, fetch included,
took 1.5 to 2.7 s and 3 to 9 s.

**Checked before it is queued.** Before any other check, `qk land` and `qk queue add` read the
messages of the entry's commits and refuse one that denies a closing claim, with nothing queued and
exit 1, naming the commit and line and what to write instead. On 2026-11-07 "This does not fix #43."
closed vocaro/BreezEPUB#43 when it reached main, two hours before the real fix
([#286](https://github.com/vocaro/vocaro-engineering/issues/286)). A claim is one `landing.CLAIM`
matches, and it is negated by `not`, `cannot`, `n't`, `never`, `no longer` or `without` up to three
words before the keyword in the same clause. A clause ends at punctuation, at `and`, `but`, `so`,
`while`, `because`, `since` or `though`, at a blank line or list item, and at a line that opens with
a capital, so "did not start, and fixes #N", a `Refs #N` and "not only fixes #N" all pass. The check
reads text alone: a comma inside the negated clause ("does not, in fact, fix #N") ends the clause and
passes it. `--no-gate` and `--front` do not skip it.

Next, `qk land` and `qk queue add` run `qk unittest`'s own checks
before a run, the ones whose refusal ends `nothing ran`, on the entry's tree, for each queue lane on
`origin/main` whose command is `qk unittest`, or a group's one such lane, with that lane's `-s` and `-p`. In a suite that keeps
its tests off the real hosts in `host_isolation.py`, a test module that does not import
`host_isolation` at its top level, or does not parse, refuses every gate that holds it, in seconds,
with no summary and no failed test to rerun or blame, so a batch holding it bisected blind. On
2026-11-07 one such entry, queued twice unchanged, cost the toolkit's queue a bisect of 11 entries and
then of 6, and held six good entries for hours
([#280](https://github.com/vocaro/vocaro-engineering/issues/280),
[#290](https://github.com/vocaro/vocaro-engineering/issues/290)). Such an entry is refused at once,
with nothing queued and exit 1, and the refusal quotes the line the gate would print. A refusal
`origin/main` already gives, made past the queue, is not the entry's, so it does not refuse an entry
that adds nothing to it. `--no-gate` does not skip the check, since the next gate on `main` would run
nothing. A syntax error in a test module once ended `qk unittest` with a traceback; it is now a
refusal line too. The check reads the suite's files from the entry's commit, not a working tree; on
the toolkit's 45 test modules it took 0.7 to 1.2 s on this Mac at a one-minute load of about 200
(2026-11-08).

Then, after the trial integration, they run the preflight lanes the worker would run before the gate,
in order, on the entry's commits replayed onto `origin/main`, in a scratch worktree outside every
checkout: the lanes `main`'s `queue.preflight` names, each run as the replayed commit's
`queuekeeper.json` gives its command, with `QUEUEKEEPER_PROJECT_CHECKOUT` naming the project's main
checkout. Until a project gives `queue.preflight`, that is its `spelling` lane, or, when it has
none, the toolkit's `python3 scripts/qk spelling`, or the same check by the toolkit's own `bin/qk`
in a project with no launcher, unless `main`'s `queue.spelling` is `false`. They run on this Mac
with no admission, since they take seconds. A finding, exit 1, refuses the entry with nothing
queued and exit 1, quoting the lane's last 40 lines. On 2026-11-07 one EPUBLib spelling slip found by the worker cost a queue round
of about 40 minutes ([#283](https://github.com/vocaro/vocaro-engineering/issues/283)). The check is a
hint, as the trial is: the worker still runs the lane on the batch, since `main` can move in
between. It queues the entry, saying why, when it cannot be made: commits that do not apply onto
`origin/main` alone, a lane that is not one plain command (foreground, display, unlocked screen,
whole host, model, compile first or a parallel group), a run past its timeout or 300 s, whichever
is less, or an exit other than 0 or 1, such as `qk spelling`'s 2 for nothing checked. A finding
`origin/main` gives too is not the entry's, so it queues the entry as well; the round's lane then
fails on `main`'s own finding.

**What comes back to the author.** `qk queue status` and `qk queue wait` name each entry's state,
reason and log. An entry that conflicts with what is ahead of it, which the check could not see
when it was queued, comes back at once and costs the others nothing. Its reason names the files in
conflict and what it conflicts with: `origin/main` when no entry ahead changes those files; only the
entries ahead that change them, by ID, when `origin/main` still holds those files as the commit's parent
did; and otherwise `origin/main` and every entry ahead
([#247](https://github.com/vocaro/vocaro-engineering/issues/247)). Those entries may still be in their
gate: once they land, rebase onto `origin/main` and add it again. A session therefore
lands with `qk land --wait` run as a background command, whose end wakes it within seconds of a
send-back; on 2026-11-03 a session that had run plain `qk land` learned of a conflict only half an
hour later, from `qk queue status`
([#130](https://github.com/vocaro/vocaro-engineering/issues/130)). While it waits, `qk land --wait`, like
`qk queue wait`, prints a line each time its entry's situation changes, such as the round it waits behind or the Mac
its gate runs on, and a `heartbeat:` line at most every 5 minutes while nothing does, with what `qk queue status`
shows of that round. On 2026-11-12 a landing printed nothing for 90 minutes, behind a failed round and then in its
own gate on another Mac, until the owner asked why
([#41](https://github.com/vocaro/queuekeeper/issues/41)). When a batch fails, the oldest half runs next, and so on until one entry fails
alone; every entry that passes along the way lands.

**A later commit does not repair an earlier one.** The check and the round cherry-pick an entry's
commits one at a time, in order, and stop at the first that conflicts (`Worker.pick` in
`merge_queue.py`), so a branch whose final tree would apply is still refused when one of its earlier
commits does not. On 2026-11-10 a narration paragraph's branch held a commit inserting it at the
paragraph boundary a queued Quick Look entry also changed, and a later commit moving it elsewhere;
`qk land` refused the range at the first commit until the two were made one, which then passed the
gate and landed (vocaro/BreezEPUB#6). Diagnose the commit the refusal names, in `COMMIT conflicts
with` and its lines in conflict, not the branch's whole diff. Then:

- When the commits from that one on are yours alone, clean and pushed nowhere, amend or consolidate
  them so that each commit the queue replays applies in order, as with `git reset --soft COMMIT^` and
  one new commit. Rewrite no other session's commits or working changes.
- Otherwise wait for the entry ahead to land (`qk land --wait`, or the `qk queue wait` the refusal
  names) and rebase onto `origin/main`, resolving the conflict at that commit.

Either way, check that the new tip's tree is the one you meant (`git diff OLD-TIP NEW-TIP` empty when
only the history changed), run the relevant checks again, and add it through the ordinary queue.
Consolidating skips no check and gives no leave to rewrite someone else's published branch. A conflict
confined to changelog lines is the worker's to settle, as above, and needs none of this.

**A branch that goes on after a landing.** The queue lands a branch's commits under new IDs, so a
branch that gets more work after one of its landings still holds the landed commits under their old
IDs. `qk land` replays them onto `origin/main`, and one whose lines a later landed commit changed
conflicts there, so the branch is refused as a merge it cannot replay (2026-11-07, #204). Start the
next change from `origin/main`, or `git rebase origin/main`: git drops a commit whose patch is already
on main, and `git rebase --skip` passes one it stops at whose change main holds in a later form.

**A bisect's two halves gate at once.** While a bisect goes on, the newer half of its suspects gates
beside the older half, in a second checkout, `~/Development/queuekeeper-workers/NAME/NAME-beside`, on the same
`origin/main` (owner's decision, 2026-11-07, [#241](https://github.com/vocaro/vocaro-engineering/issues/241)).
The two run on different Macs, whichever they are (owner's direction, 2026-11-17,
[#211](https://github.com/vocaro/queuekeeper/issues/211)). The older half is offered as any gate is
(`qk run LANE`): to the Mac with room and the most headroom, this Mac among them, or to whichever
admits it first. Once a Mac has admitted the older half's final lane, the newer half goes to any Mac
but that one, this Mac a candidate like every other, and only if one can start it now: with
`qk run --offload-only` when the older half runs on this Mac, and otherwise with `qk run --if-free`
and that Mac left out of the offer. The worker's log says which (`newer half: its lanes go on a Mac
other than build-host if one can start it now`). The newer half loses nothing by waiting to learn the
older half's Mac: it runs the same lanes, so a Mac that could start it sooner would have taken the
older half. An older half that ends before any Mac admits its final lane, as when an earlier lane
fails, leaves the newer half with no verdict, which queues it again. Until 2026-11-17 the older half
ran only on this Mac (`qk run --here`) and the newer never on it. A lane of the older half that needs
an unlocked screen then waited for this Mac's screen, 67 minutes on 2026-11-17 while the other Mac
read unlocked and had finished the newer half, until
[#205](https://github.com/vocaro/queuekeeper/issues/205) offered it to the farm's Macs in that one
case; offered as any gate is, it now goes to a Mac whose screen reads unlocked by the rule every lane
follows. Whichever half passes lands,
so a newer half that passes can land before the older one. When both pass, the older half lands first,
and the newer one lands on top of it only if its result carries over; otherwise it gates again. When
both halves fail, the older one is bisected first. The newer half instead waits behind the older one,
as before, when:

- no Mac but the older half's can start it now, or the Mac that takes it gives no verdict;
- a lane runs only on this Mac (a `self` lane, or one that says `here`; a light lane is offered as
  every lane is since 2026-11-07);
- the round is docs-only, ungated or `--front`, or a newer entry was added with an option; or
- a newer entry applies to `origin/main` only on top of an older entry.

Under #231 the rerun-alone check runs on each failed half, so the failure that splits a half is a
real one, and a half whose real failure blames one entry sends it back instead of splitting (below).
With [two farm Macs or more](shared-host.md#the-farms-macs), the newer half goes to the Mac
with room and the most headroom of those left, as any lane does.

**Entries that arrive during a gate gate on top of it.** While a batch gates, the worker checks every
`STACK_POLL` (30 s) for entries that have arrived behind it. A docs-only entry among them is never
stacked: it lands beside the gate on the docs lanes instead (*A docs-only entry never rides a code
gate*, below). Once the batch's final lane has secured admission,
the worker gates those entries stacked on the batch's tip, in the other checkout and on a Mac other than
the one that admitted it (owner's decision, 2026-11-07,
[#241](https://github.com/vocaro/vocaro-engineering/issues/241)). Earlier
lanes can run on a different Mac, and a dispatch can be declined before admission. The stacked gate is
offered as any gate is to every Mac but the batch's, this Mac among them
([#211](https://github.com/vocaro/queuekeeper/issues/211)): with that Mac left out of `qk run`'s offer
when the batch gates on another Mac, and with `qk run --offload-only` when it gates on this one. The
worker's log says which (`testing 2 entries that arrived during the gate, at TIP, stacked on BASE, on a
Mac other than build-host, which gates the batch below them`). It waits where any lane waits: in the
queue of the Mac its `qk run` started on, when that Mac is a candidate, with a place in the others'
queues if it is a whole-host lane. So in a farm with one other Mac it waits in this Mac's queue when
the batch gates on the other, as it did before under a rule that named this Mac, and when the batch
gates here it starts on the other Mac now or is tried again after four polls. With more Macs it now
waits for one, where it was offered only to start at once. The batch's push makes that tip
`origin/main`, so the next round adopts the stacked gate as its own: it gates on, and its result settles
it as any round's would, with no gate of its own to start. Anything else throws the stacked gate away,
canceling its lanes and queuing its entries again:

- the batch fails or gives no verdict;
- the batch lands at another commit (after a carry-over, or with a `Flaky-test` trailer);
- the stacked gate gives no verdict, as when another Mac declines it;
- an entry of it is removed;
- an entry goes first (`--front`, `--no-gate`, or a pass waiting for its push);
- main's lanes change;
- the queue holds; or
- the worker stops or moves to a newer toolkit.

A gate that uses the foreground waits its turn once the farm has Macs to choose among.

If no eligible Mac admits the stack's next lane, the worker waits two minutes before trying another stack on
that batch. This avoids rebuilding it on each 30-second look. If the preceding batch lands before the
decline returns, the stack's entries go into an ordinary round without counting the speculative attempt
against `queue.noVerdictRuns`.

Only one stacked gate runs at a time, so entries that arrive after it starts wait for the next round, where
they stack on it in turn. A stacked gate settles nothing itself: an entry that conflicts with the batch below
waits for its own round, and the worker's log names the entries below that it conflicts with. A bisect
already runs two halves and does not start a stack. An entry that comes back keeps its ref until
`qk queue remove ID`. Fix it, rebase and land it again.

**Removing an entry that is testing stops its gate.** `qk queue remove ID`, or `qk land` of the branch
again, takes the entry out of the queue at once. The worker looks every `REMOVAL_POLL` (5 s) for an entry
of the gate it runs that has left the queue, and then stops that gate's lanes as it does a stacked gate's
it throws away, on this Mac or the second, which ends the run there and frees its admission. It gates what
is left of the batch again, with no verdict counted and no hold, and stacks nothing on a batch an entry of
which has left, since that tip will not land (owner's request,
[#247](https://github.com/vocaro/vocaro-engineering/issues/247)). On 2026-11-07 a removed entry's gate on another
Mac held PDFReflowLib's queue for about 15 more minutes. `qk queue remove` says so of an entry that was
testing; under a worker started from a toolkit older than this, it says instead that the gate runs on until
its lanes end, at most `LANE_LIMIT` a lane, holding the queue until then.

**A failed test that passes alone is a flake.** Before it bisects a batch whose failing lane is
`qk unittest`, or a lane group holding one `qk unittest` lane whose other lanes all passed, as the
toolkit's `gate` does, the worker reruns the failed tests alone on the same batch commit (owner's decision,
2026-11-07, [#231](https://github.com/vocaro/vocaro-engineering/issues/231)). It reads their IDs from the
lane's log: unittest's `FAIL:` and `ERROR:` lines under each failed process. It first waits for this
Mac, where the reruns run, to settle, as for a retry with no verdict below. Then it runs each test by
itself, with the lane's `-s`, `-p`, `-t`, `--split`, `--batch-size` and `--timeout`, one process at a
time, up to `RERUNS` times (3). Any rerun that fails, or ends unmeasured, makes the failure real, and
the batch bisects as before unless one entry explains it (below). When every rerun of every failed
test passes, each test is a flake, and option (b) applies: the
batch lands only if none of its commits changes the test's module or a file of the project that module
imports. Only its own import statements count, at any depth of the module, looked up under the suite's
top-level directory, `src/python`, `src` and the checkout, with each package's `__init__.py`. The files
those imports import in turn do not count. A batch that changes one of them bisects. Either way the
flake is recorded in four places: in each entry's `flakes` (the test ID, the gate's log and the reruns'
logs, which `qk queue status` shows); in the worker's log; in a `Flaky-test:` trailer on a landed tip,
one per test, which survives a carry-over; and on the tracker. For the tracker the worker searches the
project's open issues for the test's name just before it writes, then comments on the first match, or
files a hardening issue. A tracker gh cannot reach is noted in the entry and stops nothing. The comment
or issue quotes the test's failure from the gate's log, beside the logs' paths: its first `FAIL:` or
`ERROR:` block there, heading, traceback and assertion, cut as a send-back's excerpt is, at most
`FAILURE_LINE_LIMIT` (500) characters a line and `FAILURE_EXCERPT_LIMIT` (4000) in all. A longer block
keeps its heading and the lines nearest its end, where the assertion is. The queue keeps only its newest
logs, and an agent in another worktree cannot read them, so before 2026-11-13 an issue that named only
the paths could outlive them (vocaro/queuekeeper#128).

**A test the batch adds is no flake.** A failed test that passes alone but that `origin/main` does
not have has no history to be a flake by, so the worker records it nowhere, files no issue and does
not land the batch ([vocaro/queuekeeper#129](https://github.com/vocaro/queuekeeper/issues/129)). A
test is the batch's own when its module, under the suite's top-level directory, defines its class and
method in the batch, and `origin/main` has no such module or a module without them. A test that a
base class gives, or that is made at run time, cannot be told this way and is judged as any other.
When every failed test that passed alone is one the batch adds, the worker sends back the entry that
adds them, saying that a test it adds failed the gate and passed alone and must be fixed before the
entry lands, and queues the rest to gate again at once. That entry is the one whose commits change
the test's module, or, among several, the one whose own tip defines the test; a lone entry is the
one. Any doubt bisects, as for a real failure: no entry or several that add the test, or added tests
that come from different entries. A test of `origin/main` that passed alone beside an added test is
a flake, recorded and filed as any other, but then no one entry explains every failed test, so by
the blame decision below no entry is sent back and the batch bisects, which finds the entry that
adds the test. Whether to relax that decision is open in
[vocaro/queuekeeper#88](https://github.com/vocaro/queuekeeper/issues/88). A batch that changes an
older test without adding it is judged as before, by its module and imports. On 2026-11-13 a test a batch added expected a wait of "45m00s", read "45m01s" on a slow run,
and was filed as a flake to harden, although it had never passed a gate.

When adding an option to an `qk unittest` gate, check the actual lane command with the runner, the
[test coverage checker](https://github.com/vocaro/apple-engineering/blob/main/guides/app-testing.md#every-test-has-a-lane) and the merge queue's single-test
rerun. The first toolkit gate with `--batch-size` ran the suite and failed its coverage check because
the checker treated the option as unknown. A focused check of the configured command catches that
failure before the full gate. [Batching gate results](https://github.com/vocaro/vocaro-engineering/issues/145#issuecomment-6774878492).

**A real failure that one entry explains sends that entry back without a bisect.** When a rerun
confirms the failure of a batch of several entries, the worker reruns the other failed tests too, and
then looks for the one entry that explains them all (owner's decision, 2026-11-07). A failed test
points at an entry when that entry's commits change a path the failing rerun's output names, as
`qk spelling`'s `PATH:LINE:` lines do, or the test's module or a file it imports, as above. A path
counts only whole and only with a directory or an extension, so a bare word is not taken for a
file. When every failed test points at the same single entry, the worker first confirms it: in the
queue's checkout it picks the batch's other entries onto the gate's base, without that entry, and runs
each failed test once alone there. Only when each passes does it send that entry back, say in its log
which entry it blamed and why, and queue the rest to gate again at once, without the halves a bisect
costs (about 30 minutes each for vocaro-engineering). The reason names the other entries of the
batch, so that the session sent back knows what else was gated with its change. Any doubt bisects as
before: a test that points at no entry or at several, tests that point at different entries, a test
whose module the worker cannot find, a failed test that passed alone beside the real failure, a rerun
that ended with no verdict, a failed test that fails without the entry too, or another entry that does
not apply without it. On 2026-11-07 a 6-entry batch bisected for about 2 hours over one spelling
test whose output named a file only one commit changed. The confirmation came after three wrong
send-backs in November 2026: a test can read a file that neither its imports nor its failure name, as
configuration it loads or sources it scans, so another entry of the batch had failed it while blame
named the entry that changed the test's imports or its own file
([vocaro/queuekeeper#183](https://github.com/vocaro/queuekeeper/issues/183)).

**A send-back says the load and whether the entry changes the failed tests' files.** The reason an
entry is sent back with ends with the load during the failed lane and with the failed tests
([#273](https://github.com/vocaro/vocaro-engineering/issues/273)). The load is said in two parts:
the readings taken while the lane ran, which count the lane's own work, and those taken while it
waited to start, which are of other work alone and are set against that Mac's busy limit
([vocaro/queuekeeper#12](https://github.com/vocaro/queuekeeper/issues/12)). Each is of the Mac the lane's wait record
names, another Mac from the moment the lane is sent there, and only those of the Mac the lane ran on are said; the
dispatch's own record, filed only when it ends, had left a lane on another Mac with its one reading at the end
([vocaro/queuekeeper#132](https://github.com/vocaro/queuekeeper/issues/132)). A gate holds the whole
Mac and loads it heavily by itself, so a high load while it ran is no sign of other work. The tests come from the lane's
`qk unittest` summary when it has one, and each test's module and what it imports are compared with
the entry's commits, as for a flake above. Otherwise they come from the summary `qk run LANE:` ends
with (`eng run LANE:` in older logs), as a Swift gate's does: each failed test is named with the file
and line it failed at, as that summary printed it ([vocaro/queuekeeper#83](https://github.com/vocaro/queuekeeper/issues/83)).
A Swift test's file is placed in the repository by its name or path, and only when exactly one
tracked or changed file can be it; the entry's commits are then compared with that file alone, not
the code the test exercises. A test whose file cannot be placed, a summary that counts more failed
tests than it names, and a summary that names none, as when a gate miscounted its devices'
failures ([vocaro-engineering#406](https://github.com/vocaro/vocaro-engineering/issues/406)), each
say so, so that the author knows the comparison is incomplete. On 2026-11-11 two of EPUBLib's
send-backs said only that the log had no `qk unittest` summary, although one gate had named both of
its failed tests. Reruns of failed tests remain for `qk unittest` lanes alone.

**A send-back for a quick check says whether `origin/main` fails it too.** When the failed lane is
one the round runs unadmitted, a preflight lane or the docs check, the worker runs that check once
more on `origin/main` alone, in the queue's checkout, and the reason ends saying that `origin/main`
passes it, or fails it too, with how many of its `PATH:LINE:` findings the entry's run printed as
well, and the path of that run's log
([vocaro/queuekeeper#180](https://github.com/vocaro/queuekeeper/issues/180)). On 2026-11-15 two
StudyWright entries each fixed the lane the other failed on a `main` broken from outside, and five
failed over about four hours before a session saw that neither could pass alone.

**A gate `qk unittest` refused sends back the one entry that brings the refusal.** When the failed
lane is `qk unittest`, its log has no summary, and it printed a refusal ending `nothing ran`, nothing
was tested, and the same tree would be refused again, so the worker reruns nothing (owner's decision,
2026-11-08, [#290](https://github.com/vocaro/vocaro-engineering/issues/290)). It looks instead for the
entries that bring the refusal: an entry whose tree alone gives a refusal naming one of the modules the
line names (the check `qk land` runs, above), or whose commits change one of those modules in the
lane's `-s` directory ([#280](https://github.com/vocaro/vocaro-engineering/issues/280)). When exactly
one entry does, the worker sends it back with the refusal line as its reason, says in its log which
entry it blamed and the line, and gates the rest at once, as for a real failure above. When none or
several do, or the line names no module, it bisects as before. When the same refusal, compared without
the checkout's directories, comes again in the next gate, or in both halves of a bisect, and no one
entry brings it, the cause is not an entry, as with a module that reached `origin/main` past the queue:
a bisect would only send back, one at a time, entries the refusal does not name. The worker then holds
the queue for a session, the entries queued again, a half of one entry among them, and the hold's reason, which `qk queue status` and
the worker's log show, quotes the line and `qk queue doctor`'s diagnosis (below). A session fixes the
cause and runs `qk queue resume`. On 2026-11-07 one entry missing the `host_isolation` import cost
bisects of 11 and then 6 entries, with every round's log naming it.

The worker bisects at once, as before, when the failing lane is not `qk unittest` (a project's own
lane or a lane group), when its log names no test it can rerun (no `qk unittest` summary and no
refusal, tests also
left unmeasured, a process with no failed test named or that ran other than it was given, or a module
that did not load), when more than `RERUN_MOST` (5) tests failed, and when this Mac does not settle
within `SETTLE_LIMIT`. On 2026-11-06 a load flake ([#230](https://github.com/vocaro/vocaro-engineering/issues/230))
split a 5-entry batch, and every entry behind it paid a 20-minute gate more. Rerunning the one failed
test alone takes seconds. That batch changed `cli.py`, which `test_devices` imports, so under (b) it
would still have bisected.

**After it lands, start again from `main`.** The round may change a commit as it puts it on
`main`, as when it keeps both sides of `CHANGELOG.md`, so `main` holds it under another hash and
another patch. Rebasing the branch onto `origin/main` then replays the landed commit and
conflicts on that file, and merging `main` in makes a merge the queue refuses. Begin the next
change on that branch from `origin/main`, as with `git reset --hard origin/main` on a clean
branch of your own once `qk land` says `landed`. In a rebase already under way, `git rebase
--skip` drops a landed commit. On 2026-11-05 one EPUBLib session's rebases stopped on
`CHANGELOG.md` three times this way.

**A lane that could not start blames nobody.** `qk run` exits 127 when a lane's command never
started: the system could not start it, or the interpreter it names, such as `python3` or `sh`, was
given a first argument that names no file in the checkout. Its receipt's `outcome` is then
`not-started`, with why under `notStarted`, and its log and summary say "could not start". `python3`
itself exits 2 for a missing file, as a failure does, so `qk run` reads the file system rather than
what the interpreter prints. The worker reads the receipt, here or from another Mac's dispatch,
not the status alone, since a lane's own command may exit 127. Such a lane says nothing of the
commits, and running it again would not start it either, so the worker holds the queue at once for a
session, with every entry waiting and none sent back or bisected: the session makes the command start,
or removes the entry whose commits took away what it runs, and resumes. Until 2026-11-13 such a lane
sent the entry back as failing "on this commit on top of origin/main", as apple-engineering's first
landing was when its checkout had no `scripts/qk` ([#108](https://github.com/vocaro/queuekeeper/issues/108)).
A lane group's members are not told apart this way yet.

**A lane Queuekeeper could not start for blames nobody.** Every `qk` command first composes its
checkout's plugins and loads the project's extensions, some from folders beside the project that
other projects share. When that raises, the command prints the traceback, then a line beginning
`queuekeeper: could not start:` with the error, and exits 70 (`EX_SOFTWARE`) before it reads its
arguments: no lane ran, and no commit was tested. The worker holds the queue at once for a session,
as for a lane whose command could not start, with every entry waiting and none sent back or bisected.
It reads more than the status, since a lane's own command may exit 70: the run must also have left no
receipt of the lane, which `qk run` writes for every lane whose command it reaches. On this Mac that
is no `source.json` in the lane's folder since the lane began; on another Mac, the dispatch's
work stage exited 70 and returned no receipt. The hold's reason quotes the error and names the Mac.
The session brings Queuekeeper and the folders the extensions load from to commits that work
together on that Mac, or removes the entry whose commits changed what the project loads, and resumes;
`qk queue resume` starts a worker from the current checkout, which is often the whole repair. On
2026-11-15, between 00:29 and 00:54, the shared vocaro-engineering checkout moved to a commit whose
adapter refused the older Queuekeeper that EPUBLib's and BreezEPUB's workers ran. Every lane ended in
that traceback with exit 1, as a failed test does, and the queues bisected their batches and sent at
least nine entries back as failing "on this commit on top of origin/main"
([#158](https://github.com/vocaro/queuekeeper/issues/158)). A lane run by a Queuekeeper from before
this change still exits 1 there, and is still read as a failure.

Before it holds, the worker asks whether the batch itself is the cause, since an entry may change
`queuekeeper.json` or an extension so that nothing loads. It starts Queuekeeper in its own checkout
on this Mac, with `--version`, which loads the extensions and runs no lane, so it takes seconds and
no admission. When Queuekeeper starts without the batch's commits and not with them, the batch is at
fault: it is bisected and the entry sent back as for any failed lane, and the queue is not held. When
it starts with neither, no entry is the cause, and the queue holds as above. When the lane ran on another
Mac and Queuekeeper starts here with the batch's commits, the fault is in what that Mac holds,
and the queue holds. A start that does not end within 60 seconds (`START_LIMIT`) says nothing, and
the queue holds. The hold's reason says which of these was found.

**A run with no verdict blames nobody.** Exit 3 (unmeasured), 69 (a worker that lost Local Network
access, on which it exits instead of counting the run), 75 (no admission) and 78 (a misconfigured host) say nothing
about the commits. The worker runs the lanes again, and holds the
queue, with every entry waiting, once three runs in a row have given no verdict, as it also does for a
push refused for any reason but a race. On 2026-11-04, 8 of 12 of EPUBLib's gates on another Mac
gave none, as the simulators' voices were not ready in time
([EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946)), and with a single retry each pair held
the queue until a person resumed it. A project sets another bound on `main`, as
`"queue": {"noVerdictRuns": N}`, a positive integer; 1 holds at the first. A run with a verdict, a pass
or a failure, starts the count again, and so does `qk queue resume`. A run that a locked screen left
without a verdict does not count (the owner's decision, 2026-11-08,
[vocaro-engineering#293](https://github.com/vocaro/vocaro-engineering/issues/293)): exit 3, or a lane
that reached its limit, whose last screen reading shows a lock, from its receipt's `screenAfter` or
else its log's last `screen:` line. The worker logs that reading, and runs that lane next as
`qk run --unlocked-screen LANE`, which places it as `"unlockedScreen": true` does: only on a Mac
whose screen reads unlocked, waiting while neither does. A run that waits out the overflow that way
exits 75, which counts, as does any run that gives no verdict without a lock.

A run that ran out of disk space holds the queue at once, with every entry waiting and none sent back
([#162](https://github.com/vocaro/queuekeeper/issues/162)): `qk run` stops a command that the disk runs short
under and ends any lane that a full disk failed as unmeasured, exit 3, with a line that begins
`queuekeeper: out of disk space:` (*Free disk space* in [the shared host guide](shared-host.md#free-disk-space)).
Another run would fill the same disk, so the worker does not retry it; the hold is for a session, which frees
space with `qk disk --delete`, or by removing what `qk disk` lists, and resumes the queue. A lane that waited
for free space before it started and exited 75 counts toward `queue.noVerdictRuns` as any run without
admission does, and the hold then says it waited for disk space.

The uncounted runs have a cap of their own, so that a lane whose screen locks during every run does not
retry without end (the owner's decision, 2026-11-09, the same issue). `LOCKED_SCREEN_RUNS`, 5, a number that
is his judgment and not a measurement, is how many runs in a row may read unlocked at their start, by
their placement or their log's first `screen:` line, and locked by their end before the queue holds. A run
that began behind the lock is not counted, since the next run goes only to an unlocked Mac. A run with a
verdict or a no-verdict run of another cause starts this count again, and so does `qk queue resume`. A run
that waited for an unlocked Mac and exited 75 ran nothing, so it counts toward `queue.noVerdictRuns` as above
but neither adds to this count nor ends it. The hold is for a session: its reason names the lane and the last
screen reading, with the Mac that reading is from, and a session can read why the screen locks during the
runs, such as a lane that holds no assertion locking by idle, and change the lane.

The `queue.noVerdictRuns` hold's reason says how many
runs gave no verdict, on which Mac the last ran (`on this Mac (NAME)` or `on NAME`, the Mac's name in the farm, when
its receipt or dispatch says), and why it gave none: the lane that reached its limit, the number of tests
its log reports unmeasured with the first of them and the last screen reading queuekeeper logged
(vocaro-engineering#185), the error its receipt records, no admission, or the host's configuration. A log
that reports no test unmeasured and whose receipt records no error gives the project's own reason when it
has one, a line that begins `Unmeasured:` ([lanes](lanes.md)): the reason quotes the last such line, at
most `UNMEASURED_SHOWN` (300) characters of it, as a test's line is bounded, and so does `qk queue status`, which shows
the hold's reason. The worker's own `no verdict (exit 3), run N of at most M` line in its log ends with the
same cause, so it is there before any hold. On 2026-11-12 mpm's gate named the timing check that voided
itself in a line of its own, `NON-RUN: ...`, while its hold said only "with no test its log names"
([#30](https://github.com/vocaro/queuekeeper/issues/30)). When the last run ended unmeasured
in a lane that takes one slot of heavy-work admission and is not `"wholeHost": true`, the reason adds
that other heavy work can join the lane after it starts, since admission looks only at a run's start, and
points a timed part that needs a quiet Mac at `qk admit --here --exclusive` from a `self` lane
([heavy-work admission](shared-host.md#heavy-work-admission)). On 2026-11-12 mpm's gate, admitted on a
quiet Mac, voided its own timing five times as other work started beside it
([#28](https://github.com/vocaro/queuekeeper/issues/28)). A hold stays
until whoever its reason names has dealt with its cause, or found it gone, and run `qk queue resume`:
the owner only when the last run's exit was 78, a shared host's configuration (a Mac whose `provision`
lane failed before the lane is such a host, this Mac included, and the reason names it and that lane's
log), and otherwise a session,
as [a hold says whom it is for](#the-merge-queue) below. Nothing fails over to another route.

Each retry waits first for the host to settle, by the test heavy-work admission applies before it takes
a slot beyond the guaranteed ones (`resources.pressure`; [heavy-work admission](shared-host.md#heavy-work-admission)):
a CPU busy share below `HEAVY_BUSY_LIMIT`, 90% by default and 70% where setup wrote it, and memory pressure at level 1 (normal); the
one-minute load average judged it until vocaro-engineering#200. A retry that started at once met the load that voided the run it repeated:
on mpm's queue, two gate runs of about an hour each passed every row but voided a timing anchor at loads
of 84 to 222, and the queue held until a manual resume once the load had fallen
([#124](https://github.com/vocaro/vocaro-engineering/issues/124)). The worker looks every 15 seconds
(`SETTLE_POLL`) for up to an hour (`SETTLE_LIMIT`), no longer than the gate run the wait may save. Past
that it holds, with every entry waiting, whatever runs the bound has left, and the hold names the
reason it last read, such as `the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=90% (this Mac's limit, the
default, since PATH does not set it)`, which says whose limit it is and where it comes from: the Mac's limits file
by its path, the environment, or the default (vocaro/queuekeeper#29). Its log names the
reason when the wait starts, again every ten minutes or when the reason changes, and at the end how long it waited, how many
looks it took and the first, highest and last one-minute load. A stop request (SIGTERM, SIGINT or SIGHUP)
ends the wait at its next look, with the entries still waiting. With another Mac configured, the test
reads the Mac the retry could use, not only the one that voided the last run: on 2026-11-07 a wait settled
build-host for 2304 s, and the retry went to this Mac by first-come admission
([#253](https://github.com/vocaro/vocaro-engineering/issues/253)). The Mac is chosen as the run starts, so
each look reads each Mac's capacity for the lane as `qk run`'s offer does (`remote.retry_macs`, the
log saying which it reads and why): another Mac when it has spare capacity for the lane, this Mac when
only it has, and both, each of which must settle, when neither has and the lane will go to whichever
admits it first. A lane that stays on this Mac reads this Mac. The choice is only as good as that reading:
another Mac can still decline an offer its capacity allowed, as it did that day, and the retry then
goes to this Mac unsettled. Without another Mac to choose, the test reads the Mac the lane ran
on, since a lane that another Mac ran left its load there
([#140](https://github.com/vocaro/vocaro-engineering/issues/140)). The worker knows it from the
lane's dispatch, which `qk run` files under `.build/queuekeeper/remote/.dispatches/LANE/` in the
queue's checkout: one that started during the run and reached another Mac, rather than only
preparing, names it. Each look asks another Mac over SSH, as `qk doctor` does, for its
CPU busy share, the `HEAVY_BUSY_LIMIT` it sets there (90% by default) and where it sets it, its one-minute
load, which the log records, and its memory pressure, and judges them as admission does; the log and the hold
name that Mac, and its limit as that Mac's (`HEAVY_BUSY_LIMIT=90% (build-host's limit, set in PATH)`). A second
Mac that cannot be reached, or does not answer with its reading, gives this Mac's reading for that look
instead, and the log says so when that starts and again once it answers. A lane that ran here, or that
no Mac ran, reads this Mac only. A lane that fails blames its commits at once, with no wait, and the
hook's no verdict waits the same way before it runs again, reading this Mac, where the worker runs the
hook.

**A hold says whom it is for, and a session resumes it.** Every hold's reason begins with its kind.
`for the owner, since nobody else can fix it` is for a cause only he can fix: a shared host's
configuration (exit 78 past the bound), a push the remote refused, a key or token a remote refused. `for
a session, not the owner` is every other: a host that stayed busy or tests left unmeasured past the
bound, a screen that locked during `LOCKED_SCREEN_RUNS` runs in a row, the pre-push hook's refusal or its no verdict, a path where the worker's checkout goes, a `main`
whose `queuekeeper.json` the toolkit at `origin/main` refuses (the reason names the toolkit commit that
judged it), a remote out of reach past the tries below, and anything unexpected. The reason ends with
who runs `qk queue resume`: the session that has dealt with the cause, or any session that finds it
gone, runs it itself and says so, in its report, and never asks the owner for it. The owner is asked
only for a hold that says it is his, and once he has fixed its cause any session that finds the cause
gone resumes (owner's decision, 2026-11-04, [#173](https://github.com/vocaro/vocaro-engineering/issues/173): "why do you need my approval to resume the queue?
this should never need my approval"). `qk queue status`, `qk land --wait` and `qk land` print the
reason with its kind. A hold whose cause a session cannot find is still a session's: say what was
looked at, and what remains, rather than asking the owner to resume.

**A closed queue takes no new entry and finishes what it has.** Maintenance that needs an empty
queue, such as a move of its state, a launcher change or a host going away, runs
`qk queue close --why TEXT`, waits for `qk queue status --idle` to exit 0, does its work and runs
`qk queue reopen` ([#32](https://github.com/vocaro/queuekeeper/issues/32)). At a cutover on
2026-11-12 a session read nine queues as idle at about 06:50, another session queued an entry at
06:57, and retiring the launcher stopped that entry's gate 23 minutes in: an idle reading does not
stop the next entry. The closure is `queuekeeper-queue/closed.json`, with why, who and when, and
`qk land` and `qk queue add` refuse with those, exit 1, before the conflict check and again under
the lock that saves an entry, which `close` takes too: an entry is either saved before `close`
returns, and `--idle` counts it, or refused. Every new entry is refused, whatever its kind:
`--front`, `--no-gate`, and a branch's newer commits, whose earlier entry stays and finishes rather
than being replaced. A commit the queue already holds is no new entry, so `qk land` of it follows
that entry, and `--front` or `--no-gate` given with it still move it, since they bring nothing in.
The worker does not read the closure. Every kind of round goes on as it would: a batch, a stacked
gate, which takes only entries queued before the close, a bisect's halves, an entry alone, and a
`--front` entry already queued. A closure is not a hold: the queue keeps working, `qk queue resume`
leaves it closed and `qk queue reopen` clears no hold. A second `close` changes nothing and exits 1,
naming the first, so that no session reopens a queue under the one that closed it; whoever closed it
reopens it, and a session it refused waits. A `closed.json` that cannot be read closes the queue
too.

**A remote out of reach is tried again.** A fetch or push that never reached the remote, or whose
connection ended, says nothing of the commits, and a brief outage clears by itself: a name that did not
resolve, a connection refused, timed out, reset or dropped, over HTTPS or SSH. The worker tries it again
after 10, 30, 90 and 270 seconds (`BACKOFF`); when the fifth try fails too, it runs the round again after
1, 5 and 15 minutes (`CLEARING_WAITS`), staying the worker through the waits, with every entry and a
batch's pass kept, and holds for a session only when the last round's fifth try fails too, about an hour
after the first ([#173](https://github.com/vocaro/vocaro-engineering/issues/173)). A round that went through starts the tries again. So a gate
that passed is not discarded because GitHub could not be reached for a moment
([#113](https://github.com/vocaro/vocaro-engineering/issues/113)). `qk land`'s own fetch retries the
backoff the same way. Git's message decides: one that says the remote answered and refused, an authentication
failure, a 4xx status, an SSH key or host key refused, a missing repository or a rejected push, holds
at once, for the owner, though SSH ends a refused key with the same `Could not read from remote repository` as a
dropped connection. Any other error holds at once, for a session.

**A push refused for its credentials is tried again.** During GitHub's incident of 2026-11-04 its
SSH refused the owner's key from one second to the next for over an hour, while the same key fetched
([#151](https://github.com/vocaro/vocaro-engineering/issues/151)). So a push refused for its key or
token (`Permission denied (publickey)`, `Authentication failed`, `Invalid username or token`), when
the fetch just before it went through from the same URL, is tried again after each of `BACKOFF`. On
GitHub the worker then reads [its status](https://www.githubstatus.com) for Git Operations: while that
is not operational, it tries every minute for up to an hour (`SETTLE_LIMIT`), since a hold costs a
gate. A refusal while Git Operations is operational, past the hour, with a status that could not be
read, or on another host holds, and the hold's reason gives GitHub's status. A push URL other than
the fetch's, a refused fetch, and any other refusal hold at once.

**A pre-push hook runs ahead of the push.** `git push` connects before it runs the hook, so a hook's
long wait held an SSH connection open until GitHub dropped it. After a gate passes, the worker runs
the project's hook itself with `git hook run`, as Git would call it for pushing to `main`, with its
log in `queuekeeper-queue/logs`, and then pushes with `--no-verify`. A connection that dropped or a race that
leaves the commit unchanged pushes again without running the hook again; a carried-over commit is a
new commit and runs it again. A hook's exit 3, 69, 75 or 78 is no verdict, as a lane's is: it is tried
once more, once the host has settled, and then holds. Git passes the hook's status through, so a hook that waits for admission
should exit with the status it got rather than 1. Any other refusal holds at once, for a session, since the gate
passed and the hook disagrees, which is for a session to read; it blames no commit.

**A hook that repeats the queue's lanes may be covered by them.** A project opts in on `main`, naming
each lane that covers the hook and what of the hook it runs. A branch cannot opt itself in, since the
setting is read from `main`:

```json
"queue": {"lanes": ["gate", "app-gate"],
          "hookCoveredBy": {"gate": "check-all.sh, the hook's first step", "app-gate": "check-app.sh, its second"}}
```

The lanes must be ones the queue runs, so their passing is what the worker just saw, and a wait for
admission there is a lane's no verdict. The worker then does not run the hook, and its log says so.
Keep the opt-in only while it is true: a hook that checks something no lane does stays a hook. A person's own `git push` still runs the hook.

**Queuekeeper supplies a hook that runs the lanes.** A project whose hook would only repeat its
lanes can make the hook one line:

```sh
#!/bin/sh
exec python3 scripts/qk pre-push "$@"
```

`qk pre-push` reads what Git gives a hook and finds the paths the push changes. A push that changes
only paths no code lane reads, by `queue.unread` and its Markdown defaults, runs the
docs lanes. Any other push runs `queue.lanes`, or `gate`, after those docs lanes when it changes
such a path beside others. So the hook holds no second copy of what
the configuration says. A push that changes no path runs nothing and says so: its files are ones the
remote already has, as when a merge keeps a landed branch's commits and has the remote's own tree
([#165](https://github.com/vocaro/queuekeeper/issues/165)). When Git cannot list the paths, the
push runs `queue.lanes`, or `gate`. The lanes run with `qk run` in the checkout being pushed from, and are
admitted and placed as always.

| Hook's exit | Meaning |
|---|---|
| 0 | Every lane passed, or the push deletes a branch or changes no path |
| 1 | A lane failed |
| 3 | A lane was unmeasured: nothing ran, which is not a pass, so the push is refused |

`QUEUEKEEPER_PREPUSH=skip git push` runs nothing and says that nothing was verified. Under the
queue's worker the hook runs nothing, since the worker has just run the lanes for what it pushes.
Such a project does not need `queue.hookCoveredBy`.

**`qk doctor` keeps `core.hooksPath` right.** A project that tracks its hooks points
`core.hooksPath` at their folder, as a path relative to the checkout. Git resolves a relative path
in whichever worktree runs the hook, so each worktree runs its own branch's copy. Three states break
that, and Git reports none of them:

| State | What happens | What `doctor` does |
|---|---|---|
| The repository's value is an absolute path into one checkout | Every worktree runs that checkout's hooks, not its own branch's | Makes it relative |
| A linked worktree has a value of its own, which hides the repository's. Claude Code writes one, aimed at the main checkout, in each worktree it creates | A push from that worktree runs the main checkout's hooks | Clears it when it names the same folder, and warns when it names another |
| The path names a folder that does not exist, or a hook is not executable | Git runs no hook, prints nothing, and the push succeeds | Warns |

Each repair is listed among `doctor`'s warnings. A project with no `core.hooksPath`, or with one
outside the repository such as a person's own global hooks, is left alone.

A fresh clone has no `core.hooksPath` at all, so it runs no hook until someone sets it, and Git
says nothing. A project names its hooks folder in `queuekeeper.json` so that `doctor` sets it:

```json
"gitHooks": "tools/repository/hooks"
```

With `gitHooks`, `doctor` sets `core.hooksPath` to that folder where it is unset, and warns where it
names another folder, a person's own global hooks among them, or where the folder is missing.
Without the key, a tracked `pre-push` in a folder whose name says hooks and which runs
`qk pre-push` is taken for the project's own: `doctor` warns that the path is unset and changes
nothing. Any other project with no `core.hooksPath` is left alone, as before. This replaced the part
of vocaro/mpm's `install-hooks.sh` that set the path on a fresh clone (vocaro/queuekeeper#131,
2026-11-13).

**`qk doctor --hooks` is the quick form for a session-start hook.** It checks and repairs
`core.hooksPath` alone, as above, and nothing else `doctor` checks, in about a second. It prints one
line for each finding, each beginning `repaired` or `warning:`, or one line beginning `fine:`:

| Exit | Meaning |
|---|---|
| 0 | Nothing was wrong, or everything wrong was repaired |
| 1 | A warning is left, which the person must act on |

A Claude Code `SessionStart` hook runs it as it is, and need not read `doctor`'s JSON:

```json
{"type": "command", "command": "python3 scripts/qk doctor --hooks", "timeout": 15}
```

**A gate that fails on a test the change cannot reach.** On an `qk unittest` gate the queue does this
itself when the test passes alone ([a failed test that passes alone is a flake](#the-merge-queue)).
Otherwise, record the failure on that test's issue,
or file one by the project's labels, before adding the entry again: an entry added again with no
record leaves the flake nobody's to fix. On 2026-11-05 the fix for
[EPUBLib#978](https://github.com/vocaro/EPUBLib/issues/978) went through EPUBLib's queue four
times: once on its own regression test's race, then twice on Mac display checks that ran on another
Mac beside another project's gate
([EPUBLib#875](https://github.com/vocaro/EPUBLib/issues/875),
[EPUBLib#896](https://github.com/vocaro/EPUBLib/issues/896)), each recorded on its issue, while
the iOS lane passed every time. The options below are for a fix that cannot wait, not for a flake.

**Landing ahead of the queue.** Two options of `qk land`, and of `qk queue add`, are for
emergencies only, never routine. They work in every repository, with no setting (owner's decision,
2026-11-04, [#163](https://github.com/vocaro/vocaro-engineering/issues/163)), and do what a hand push
past the queue did, with the tool keeping the record:

```sh
python3 scripts/qk land --front --why "the toolkit fix every queued change needs" --wait
python3 scripts/qk land --no-gate --why "an unrelated flaky test fails the gate" --wait
```

- `--front` tests the entry next, ahead of everything waiting, as a batch of its own. A gate already
  running finishes first, as does a batch that passed and waits for its push; a bisect in progress
  goes on where it was. `qk land --front` again with a commit already queued moves that entry.
  `--why` is optional here, and the entry and `qk queue status` show it. Example: an important
  toolkit fix stuck behind several queued changes jumps the queue. The entries already queued are
  then tested with the fix without being queued again: they rebase onto the new `main`, and other
  projects' workers move to the toolkit at `origin/main` each round.
- `--no-gate` needs a written reason, `--why TEXT`, and is refused without one. It lands the entry
  alone, in its turn: the worker rebases it onto `origin/main`, runs only the project's preflight
  lanes, as above, and the pre-push hook, unless `main`'s `queue.hookCoveredBy` says the gate
  covers the hook, and pushes. The reason is recorded everywhere the skip is: the entry's `ungated`,
  with the lanes; the worker's log; the landed tip's `Landed-without-gate:` trailer, its author,
  message and patch otherwise its own; and `qk queue status`. Example: an important fix is held up by
  an unrelated flaky test failing the gate. It is no gate of the pushed commit, so `eng release`
  will not take it. The next gate on `main` covers it: `qk queue status` lists it as `ungated on
  main` until a landing whose gate ran on a base that holds it has passed, and a docs-only landing
  does not count. A session that uses it tells the owner it did and why, as for a hand push.

With both, the entry lands next, ahead of any other `--front` entry, which is the hand push of
2026-11-04: a one-line change that made a late voice listing a skip on simulators, so that
EPUBLib's gates gave a verdict again ([EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946)).
It does not wait for a round whose gate has not started: while the lane a gate runs waits for
admission, an unlocked screen or a Mac, by its wait record (*A deep queue*, below), the worker stops
it within `REMOVAL_POLL` (5 s), as for a removal, lands the entry, and gates that round's entries
again on top of it, a bisect where it was. This holds for each gate the worker runs: a batch, a
bisect's halves, a `--front` entry's and a stacked one. A gate that has started finishes first, and
`qk queue status` says so under the entry: `waits for the running gate of ID, which has run Nm`. On
2026-11-12 such an entry waited more than 10 minutes behind a StudyWright round whose gate waited for
admission behind another project's whole-host gate
([#26](https://github.com/vocaro/queuekeeper/issues/26)). The same holds for an entry already
testing in such a round that `qk land --front --no-gate` is given again: it leaves the round, which
gates again on top of it. On 2026-11-15 `--no-gate` given again to an entry of a stacked gate that
no Mac admitted for 30 minutes changed nothing, and only `qk queue remove` and landing it again
landed it. Any other option given again to a testing entry still changes nothing, and `qk land`
says how to land it alone without the gate.

**Pushing by hand is the exception.** A session pushes to `main` by hand only when `qk land` cannot
be used, when it must be bypassed, each for a reason it can state, such as a broken queue or a fix
that cannot wait for it, or with the owner's approval. A fix that cannot wait for the gate lands
with `qk land --front --no-gate --why TEXT` instead, as above. A record or a document lands
by the queue like anything else. A hand push follows [push only onto the `main` you tested](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/maintenance.md#concurrent-git-integration)
and is reported to the owner with its reason. No check enforces this.

**Paths no code lane reads.** One setting on `main`, `queue.unread`, says which paths no code lane
reads, and the queue asks it two questions: whether a batch may skip the gate, because every path it
changes is unread, and whether a passed result stands when `main` moves, because every path `main`
gained is unread and untouched by the batch. One list answers both, so a document never skips the
gate yet fails to carry over, or the reverse. The conventional Markdown defaults below count unless
`defaults` is false:

```json
"unread": {"paths": {"doc/guide/": "read by no code lane; qk docs check reads it",
                     "records/": "landing records; no lane reads them"},
           "exclude": {"doc/guide/input.md": "a code test reads this fixture"},
           "lanes": ["docs"]}
```

Every key is optional. `paths` adds to the defaults, each a file or a directory ending in `/`, with
why no code lane reads it. A directory there takes every file in it, not only its Markdown as the
defaults do, so a file of another kind in it, such as an image a document shows, needs an
`exclude` entry or a place outside it. `exclude` names paths among them that code does read, and wins over both.
`lanes`, by default `["docs"]`, is what a batch of unread paths runs in place of the gate, never
`gate`. `defaults: false` drops the Markdown defaults. `"unread": false` gates every batch and
carries nothing over. List a path only when no lane reads it, and check first: studywright's gate
compiles sources under `measurements/audits`, and BreezEPUB's script tests read parts of
`evidence/`. A path left out costs a gate run; a path wrongly listed lands untested.

A path that one or two small checks read, and no other code lane, can name those checks' lanes in
place of a plain reason (vocaro/queuekeeper#27):

```json
"paths": {"doc/release/": {"why": "only the privacy checks read it", "lanes": ["privacy-docs"]}}
```

A docs-only batch that changes such a path runs `lanes` plus every lane its changed paths name,
never the gate, and its landing records them as `docsNamed`, each with the paths that named it and
why, which `qk queue status` and `qk queue result` show. Since those lanes read the path, `main`
gaining it never carries over a passed result: the batch gates again. A batch that changes such a
path beside code rides the gate as any other.

`queue.unread` replaces `queue.docsOnly` and `queue.carryOver`, which said the same thing in two
lists that could disagree. A release reads them as `queue.unread`: `carryOver`'s paths join
`docsOnly.paths`, and an explicit `docsOnly` keeps the defaults off, as it replaced them. `qk doctor`
and `qk land` warn of them and print the `queue.unread` to give in their place; the next release
refuses them. Neither may stand beside `queue.unread`. `docsOnly: false` beside a `carryOver` has
no exact reading: until it goes, every batch gates and the `carryOver` paths still carry over, and the
warning asks the owner to choose `"unread": false` or those paths as `unread.paths`. Two things change on reading: Markdown outside the defaults, such
as a root `AGENTS.md`, no longer carries over unless `paths` names it, and a batch of only
`carryOver` paths runs the docs lanes rather than the gate.

**Main moving under a passed batch.** The queue takes a hand push as it comes. If `main` moved only
by unread paths the batch does not touch, the worker checks that the batch's patch is unchanged on
the new `main`, runs `qk docs check` when a document moved, then the project's
`queue.carryOverChecks`, and pushes. A carried-over result is no gate of the pushed commit: `eng
release` will not take it. Any other move gates again, with no blame.

**A docs-only batch runs the docs lanes.** A batch whose every path is unread is a docs-only
batch. By default, existing and newly initialized repositories use this path for root
`README.md`, `CONTRIBUTING.md`, and `CHANGELOG.md`, and Markdown files (`.md` or `.markdown`)
under `doc/` and `docs/`. `guides/` and `knowledge/` are no standard documentation folders and no
default (vocaro/queuekeeper#106): a project that keeps documents there names them in
`queue.unread.paths`, which adds to the defaults, or a change to them takes the code gate and
gates a passed batch again.
The worker runs the preflight lanes, followed by the project's `docs` lane or the toolkit's
implicit `docs` lane (`python3 scripts/qk docs check`, or `qk docs check` by the toolkit's own
`bin/qk` in a project with no launcher). Queuekeeper's own guides are under `doc/guides/`, so
the default takes them. Its `queuekeeper.json` names one path, `doc/user_guide.md`, with the
`setup-examples` lane that runs `test_setup_examples`, the one code test that reads it, so a
docs-only batch changing that guide runs the test beside the docs lanes. On 2026-11-14 a docs-only
landing removed the README's setup example, which that test then read; every later code entry's
gate failed on it, and the queue blamed the first, which touched no document
([vocaro/queuekeeper#147](https://github.com/vocaro/queuekeeper/issues/147)). A test that comes to
read another document needs the same declaration; nothing finds it.
Code, configuration, agent instructions, non-Markdown data, and documents under source or test
folders retain the code gate. A batch that changes nothing runs every code lane, and so does an
entry whose own commits change a document and code.

**The short checks run without admission.** In every round, whatever its batch changes, the
preflight lanes and `qk docs check` run in the worker's checkout
without admission, so that seconds of checks never wait behind a whole-host gate or busy slots:
a preflight lane as its own command, and the docs check implicit, or a
project lane named `docs` whose command is exactly the toolkit's. Each has `DOCS_CHECK_LIMIT`, 60 s, about 45 times the slowest
measured run (0.2–1.3 s on this Mac at a load of 15–44, 2026-11-11). One that reaches it runs
again through `qk run`, admitted, so a slow check is never a failure and never runs long
outside admission. Any other docs lane, such as a project script, runs through ordinary lane
admission, since queuekeeper cannot know its cost, and so does a lane `queue.lanes` names, the
gate, provision and every other lane. Either way the lanes leave their logs. An entry that
leaves the queue stops an unadmitted check as it stops an admitted lane. This began with
docs-only batches (vocaro-engineering#394) and holds for a batch that changes code, a bisect's
halves, a stacked gate and a `--no-gate` landing since vocaro/queuekeeper#169: on 2026-11-15 one
Queuekeeper round waited 34 min 33 s for admission to check spelling and 8 min 3 s to check
docs, about 2 s of work, before its gate began.

**A batch of documents and code runs the docs lanes too** (vocaro/queuekeeper#122). When the paths
a batch changes include an unread path, by `main`'s settings as above, beside any other, the
worker runs the docs lanes that a docs-only batch would run for those documents as well as the
gate: after the preflight lanes and provision, ahead of `queue.lanes` or `gate`, so that a cheap failure
sends the batch back before the gate starts. A docs lane that `queue.lanes` already names, itself
or as a member of a group, runs once. Both halves of a bisect run the docs lanes when either
changes a document, and a gate stacked on a batch chooses its own lanes by its own entries' paths.
`qk pre-push` does the same, with the docs lanes first. A project need not copy its docs checks
into its gate. On 2026-11-12 a vocaro/mpm commit that changed tooling and `AGENTS.md`, one of its
docs-only paths, took that file past the 32 KiB its docs lane allows. Its gate ran three
times, never held that check, and the commit landed; the next docs-only batch would have failed
on a file it did not change. `queue.unread: false` turns this off with the rest of docs-only
handling.

**A docs-only entry never rides a code gate** (owner's rule of 2026-11-11,
vocaro-engineering#394). The worker classifies each waiting entry by the paths its own commits
change, by `main`'s settings as above, and keeps docs-only entries out of code batches:

- Queued beside code entries, the docs-only entries land first, in a round of their own on the
  docs lanes; the code entries gate in the next round, without them.
- While a code batch gates, a bisect's half or an entry put first among them, a docs-only entry
  that waits lands beside the gate, in a checkout of its own beside the worker's (suffix
  `-docs`), looked for every `STACK_POLL`. Only entries that change none of the gating batch's
  paths go there; their paths are unread, so when the code batch passes, its result carries over
  the documents `main` gained (*Main moving under a passed batch*), and so does a gate stacked on
  it. A docs-only entry is never stacked on a code gate.
- A docs-only entry goes ahead of an older entry only when the two change no path in common, and
  a docs entry left in line holds back every later entry that changes one of its paths, so no two
  entries that change a path in common change order, and integration and conflict detection are
  what they were. An entry put first by `qk land --front`, or landing with `--no-gate`, still
  goes first; a docs entry does not go beside the gate while one waits.
- A docs entry that fails its docs lanes ahead of older entries may need one of them, as a link
  needs the file an older entry adds. It is not sent back: it records them as `waitsFor` and goes
  back in line behind them, then lands, or fails and is sent back, on top of them.

A project whose code or tests consume a conventional documentation path must name it in
`queue.unread.exclude`, or set `queue.unread` to `false` to keep every landing gated. Name any
project-specific documentation validators in `queue.unread.lanes`. Only `main`'s settings
count, so a branch cannot widen its own docs classification. Both a moved file's old and new paths must qualify: a move across
the code/documentation boundary runs the code gate.

Several docs-only entries land as one batch after their docs checks pass. The pre-push code
hook is skipped for a docs-only batch, including in projects without `queue.hookCoveredBy`;
put required documentation checks in the docs lanes rather than that hook. Code and mixed
landings keep their ordinary hook policy. No emergency option is needed for documentation.

`qk land` says which of the two an entry will ride as it queues it, by the same paths and
settings: `ID is documentation only: lands on LANES, not the code gate`, or `ID rides the code
gate: PATH is not documentation`, with the first such path in sorted order and, when the entry
changes documents too, the docs lanes that run ahead of the gate (`rides`, vocaro/queuekeeper#118).
An entry of a README change, a stylesheet, a script, a test and three screenshots was expected to
land as documentation and rode the full gate, which its author learned only by waiting.

A docs-only landing is no full gate of the pushed commit: `gate` may not be a docs lane, no
gate receipt names that commit, and `eng release` will not take it as a release qualification.
A release commit gets its gate from the release procedure. The worker's log and
`qk queue status` record the docs lanes actually run and say this was a documentation landing.

**Checks before a carry-over.** A project whose rule asks for more than the docs lane names the
commands in `queue.carryOverChecks`, in order, on the batch rebased onto the new `main`:

```json
"carryOverChecks": [
  {"command": ["python3", "tools/check_records.py", "--base", "{base}"], "paths": ["records/"]},
  {"command": ["python3", "scripts/qk", "docs", "check"]}
]
```

`command` is an argv list. `paths`, optional, limits a check to moves of those paths, each a file or
a directory ending in `/`, for a check too costly to run on every carry-over. Each runs through
queuekeeper's supervision, with `LANE_LIMIT` as its limit and a log of its own under `queuekeeper-queue/logs`, so
heavy work inside one goes through `qk admit` or `qk run` as anywhere else. A check that fails,
times out, cannot start, or finds no admission ends the carry-over: the batch gates again, with no
blame and no hold. A check is judged only by its exit status, so it must read what main's move
changed. Git diffs against `origin/main` read nothing of it, since by then the batch sits on that
`main`: `{base}`, anywhere in a word, is the commit the result passed on, and such a check takes it
as its base. Prove each check fails on a bad move before relying on it.

**State and recovery.** Each repository's state is under `queuekeeper-queue` in its common Git directory,
so every worktree sees one queue. A worker starts with `qk land`, apart from the session,
and exits when nothing waits. A queue set up with the [signed launcher](#signed-queue-launcher)
also checks for waiting work at login and every two minutes. After a reboot, `qk queue wait`
can start a worker, and `qk queue resume` can clear a hold and start one. After taking
the queue lock, a worker checks for dispatchers proven gone and requests a stop for each run
their records identify, including a crashed worker's gate (doc/guides/shared-host.md, How a run
reaches another Mac).
Whatever a dead worker left testing was never pushed, so the next worker tests it again. A batch
whose lanes passed is recorded `passed`, with the commit that passed and its base, before its push;
a hold the push meets, a refusal or a remote out of reach, keeps it so, and the hold's reason says so.
The next worker, after `qk queue resume`, pushes that commit without gating it again, while the
batch is still the one that passed: an entry removed or added again since gates what is left. Main
having moved meanwhile is the carry-over's decision, as for any push. A lane may
take `LANE_LIMIT` (6 h, three times queuekeeper's default limit: the default for the lane and two more for
its wait for admission). The worker's checkout is locked, so that no sweep removes it, and keeps
ignored inputs a provisioning lane made between rounds. A project with `"integration": "pull-request"`
on `origin/main`, or with no gate lane on `origin/main` or in the commit added, is refused, and the
command says what to add. Neither `qk land` nor the worker reads any checkout's working tree for
this, so a main checkout that lags behind `origin/main` changes nothing: the worker reads its settings
from `origin/main` in its own checkout, and `qk run` there reads each lane from the batch.

**A deep queue.** `qk queue status` says why entries wait, from `queuekeeper-queue/round.json`, the
worker's account of its round: whether it bisects, with how many suspects and the size of each
half; each running gate's lane, the Mac it runs on and how long it has run; what a gate waits for
and for how long, from the record `qk run` keeps in `.build/queuekeeper/.waits/LANE.json` on any
Mac (a run on another Mac sends it back as it changes, and it is cleared on admission), such as
admission on any Mac behind another project's whole-host gate, an unlocked screen, the second
Mac's foreground or another Mac that cannot be reached; a gate stacked on the batch; and a wait for
the host to settle after a run with no verdict. The record says only what a lane waits for, never
how it ended: a wait whose process is gone or that an earlier run left is ignored, and a lane's
verdict stays its receipt's. A gate held up by a dead or uncertain guard
entry that admission has not recovered, for 15 minutes (`STUCK_SECONDS`), is shown as
`stuck:`, with `python3 scripts/qk admissions recover` to list the entries, and the worker's log
says so once; on 2026-11-07 BreezEPUB's gate waited 86 minutes behind three dead entries while
status said only "waiting" (vocaro-engineering#315; doc/guides/shared-host.md, Lock reclamation). The
queue's own doctor acting on it is vocaro-engineering#290. `--json` has the same under `round`,
each gate's `waitingSince` and any `stuck` among them. On 2026-11-07,
with 3 entries testing and 6 queued, that took a dozen commands: a bisect of a spelling failure,
its older half waiting more than 25 minutes for this Mac behind StudyWright's and BreezEPUB's
whole-host gates, and another Mac answering "No route to host". For more, the answers live in
`queuekeeper-queue/worker.log`, the worker's own log; in each gate's log in `queuekeeper-queue/logs/`, named
`ROUND-LANE.log`, which `qk queue status` names; and, for a gate another Mac ran, its full
output in the queue checkout's `.build/queuekeeper/remote/.dispatches/LANE/*/run.log`, which the
gate's log also copies once the lane ends.

**A queue that is stuck: `qk queue doctor`.** `qk queue status` says what the queue is doing; `qk
queue doctor` says whether that is testing or being stuck, from signals the queue already keeps, and
for each what it means and what heals it ([#290](https://github.com/vocaro/vocaro-engineering/issues/290)).
It reads the entries, the hold, whether a worker runs, and the logs of the newest twelve gates of the
`qk unittest` lanes back to the last that gave a verdict, a pass or a failure. It skips running
gates and speculative stacks whose last lane no Mac admitted. Its signals:

- **refused gates**: gates that `qk unittest` refused before running anything (a line ending
  `nothing ran`), how many in a row with the same refusal, their logs, and the waiting entry whose
  tree alone gives that refusal. Only when exactly one entry does is it named as the cause.
- **refused before running**: a waiting entry whose tree `qk unittest` refuses, as `qk land` now
  refuses before queuing, for an entry queued before that check or by an older toolkit.
- **no verdict**: two or more gates in a row since the last verdict that printed no `qk unittest`
  summary or left tests unmeasured, such as a lane at its limit or another Mac out of reach
  ([#253](https://github.com/vocaro/vocaro-engineering/issues/253),
  [#268](https://github.com/vocaro/vocaro-engineering/issues/268),
  [#282](https://github.com/vocaro/vocaro-engineering/issues/282)).
- **came back**: a waiting entry holding the very change, by its stable patch ID, that an earlier entry
  from the same branch, or with the same subject, held when it failed or was taken out.
- **no worker**: entries waiting, the queue not held, and no worker running
  ([#287](https://github.com/vocaro/vocaro-engineering/issues/287)); **held**: the hold's reason.
- **held back on a Mac with spare CPU**: a running gate that has waited 15 minutes or more for
  admission (`IDLE_WAIT_SECONDS`) on a Mac whose CPU busy share is under its `HEAVY_BUSY_LIMIT`. It
  says what the gate waits behind, in the waiter's own refusal, and each holder of admission on that
  Mac: its command, PID, how long it has held, the CPU it and what it started use, and where its work
  comes from, its repository, branch, worktree and agent session, so that the session that owns it can
  be found and asked. On 2026-11-11 BreezEPUB's gate waited 45 minutes on this Mac, 83% idle, behind
  one slot held by another session's iPad Quick Look run at about 3% CPU, while doctor said
  `healthy`; finding the holder took `ps`, `lsof`, the slot files and a guess at the worktree
  ([vocaro/queuekeeper#89](https://github.com/vocaro/queuekeeper/issues/89)). The bound is
  `STUCK_SECONDS`'s: an idle Mac admits within `HEAVY_SETTLE_SECONDS` (30 seconds) of a slot freeing,
  so a quarter hour is no settle and no short lane ahead, and it is already three quarters of the
  20-minute gate goal. The busy share is read as the gate's busy sampler reads it, and the holders as
  `qk admissions now` reads them, over ssh for a gate that waits for another Mac. Holders that
  cannot be read are `unknown`, and a holder whose process could not be checked
  ([vocaro/queuekeeper#23](https://github.com/vocaro/queuekeeper/issues/23)) has its CPU use unknown;
  neither is guessed light. A Mac at or above its limit is busy, and its gate's wait is no finding; one
  whose busy share cannot be read gets a `note:` saying whether it has spare CPU is unknown.

It exits 0 and says `healthy` when it finds none of these, and 1 otherwise; `--json` gives the same,
and a held-back gate's finding there carries its Mac, busy share and limit, and the holders as `qk
admissions now --json` gives them.
A running gate that waits for admission for less than that bound, or on a busy Mac, is no finding;
doctor adds a `note:` pointing to `qk admissions
now` ([shared-host.md, Who holds and who waits](shared-host.md#who-holds-and-who-waits)), and so does
`qk doctor` for a lane of its checkout.
It changes nothing: no entry, no hold, no worker. The worker acts on a refused gate itself: it sends
back the one entry that brings the refusal, and runs the doctor for the reason of the hold it makes
when the same refusal comes again with no entry to blame (A gate `qk unittest` refused, above; owner's
decision, 2026-11-08, #290). For the other findings a session acts itself, as each `next:` line says.
Restarting a worker that died (#287) and moving a no-verdict run to another Mac are not the doctor's
yet.

Use [`qk queue placement`](shared-host.md#check-gate-placement-without-running-a-gate) to see
whether each configured build Mac could admit a gate lane now, even when the queue is empty.

### A project's first landing

The commit that adds a project's `queuekeeper.json` lands through the queue like any other, with
`qk land --wait`. The queue reads a project's settings from `origin/main`, so that no branch chooses
its own gate, but `main` has none before this commit. Until it has a `queuekeeper.json` that names
lanes, the queue reads the entry's when it is queued, and the batch's in a round, for that round
alone, and says so: `qk land` prints `origin/main has no queuekeeper.json yet, so the queue reads
COMMIT's for this landing alone`, and the round's log `main at COMMIT has no queuekeeper.json yet:
this round reads the batch's`. The round runs the lanes it names, its `provision` lane included, as
it would once they are on `main`.

This trusts the entry with its own gate only where `main` has no gate for it to loosen. Four settings
loosen what the queue checks rather than say what it runs, and only `main` gives them, even then:
`queue.hookCoveredBy`, which skips the pre-push hook, `queue.unread`, which skips the gate for
the paths it names and lets a passed result carry over them, with the older `queue.docsOnly` and
`queue.carryOver` it replaces, `queue.spelling: false`, which turns off the toolkit's spelling check (the
owner's decision, 2026-11-13, [#10](https://github.com/vocaro/queuekeeper/issues/10)), and
`queue.preflight`, which can name fewer checks or none
([#69](https://github.com/vocaro/queuekeeper/issues/69)). A project that
wants it off lands its configuration past the check once. Once the commit lands, every later entry is gated by `main`'s settings, a change to
`queue.noVerdictRuns` or the lanes included, and a commit that has no `queuekeeper.json` while `main`
has none is refused before it is queued.

All eight of the owner's projects went in by hand on 2026-11-12, before the queue could do this
([#10](https://github.com/vocaro/queuekeeper/issues/10)): `qk run gate` on the branch and a
fast-forward push with the owner's approval, which skipped the `provision` lane a round runs.

### Changing Queuekeeper while work runs

Do not edit, commit in or move the main checkout of Queuekeeper while a lane is being dispatched
from it. A dispatch sends a snapshot of that checkout's tree, and a run that finds the tree changed
between the send and its start does not run: "restore the tree that was sent, or run it again to
send the tree as it is now". A project's gate was lost this way on 2026-11-12. Make changes in a
linked worktree and land them through Queuekeeper's own queue, which moves the main checkout only
when nothing runs from it.

Never switch the main checkout to another branch either: every project's launcher loads Queuekeeper
from it, so each `qk` run by hand would run that branch's code. While the Queuekeeper a `qk` loaded is a
main checkout on a branch other than `main`, or detached at a commit neither `main` nor `origin/main`
holds, every command says so first on standard error, `queuekeeper: Queuekeeper loads from PATH, on
branch NAME at COMMIT, not main, ...`, and `qk doctor` lists the same among its warnings. A linked
worktree, such as a session's or a queue worker's copy, says nothing. On 2026-11-15 a session left the
checkout on its work branch for 45 minutes, unnoticed
([#184](https://github.com/vocaro/queuekeeper/issues/184)).

### Copies in each project

Two files in every project are copies: `scripts/qk`, from this repository, and, in the owner's
projects, the toolkit's `scripts/_engineering.py`. A fix to either reaches a project only by a
landing there. On 2026-11-12 two fixes to them took two rounds of landings across seven projects,
each a full gate. Before changing either file, collect the changes into one, and expect one landing
per project ([#20](https://github.com/vocaro/queuekeeper/issues/20)).

### Signed queue launcher

Set this up once on the Mac that owns the queues. From the Queuekeeper checkout, build the app in its
build directory, then sign it in your Terminal with an Apple-issued signing identity.

```sh
app="$PWD/.build/Queuekeeper.app"
mkdir -p "$app/Contents/MacOS"
cp src/python/queuekeeper/_native/queue-launcher/Info.plist "$app/Contents/Info.plist"
python3 scripts/qk admit --here -- /usr/bin/clang -std=c11 -O2 -Wall -Wextra -Werror \
  src/python/queuekeeper/_native/queue-launcher/main.c -o "$app/Contents/MacOS/QueueLauncher"
/usr/bin/codesign --force --sign "Developer ID Application" --timestamp=none "$app"
/usr/bin/codesign --verify --strict "$app"
```

Install the app at a stable path and register it with Launch Services. When macOS prompts for
Local Network access, allow Queuekeeper. If the app is blocked from reaching another
Mac, check its access in System Settings under Privacy & Security, then Local Network.

```sh
mkdir -p "$HOME/Applications"
/usr/bin/ditto "$app" "$HOME/Applications/Queuekeeper.app"
app="$HOME/Applications/Queuekeeper.app"
/usr/bin/open -g -a "$app" --args /usr/bin/true
```

For each queue, run the following from its main checkout using the Python installation and
`PATH` its worker should use after login. Use the account's normal Git and SSH configuration for credentials.

```sh
draft="$(mktemp)"
python3 scripts/qk queue launcher-plist "$app" > "$draft" &&
  label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$draft")" &&
  mkdir -p "$HOME/Library/LaunchAgents" &&
  install -m 644 "$draft" "$HOME/Library/LaunchAgents/$label.plist" &&
  /bin/launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
rm -f "$draft"
```

`qk land`, `qk queue wait` and `qk queue resume` ask launchd to start the installed agent.
At login and on its interval the agent runs the same worker entry point. Empty and held queues
return before recovering dispatches or running gates. The queue lock excludes duplicate workers.
If launchd rejects a start request, `worker.log` records the error and queuekeeper starts the session
worker. To remove the setup, close the queue to new entries, wait for it to be idle, run
`/bin/launchctl bootout "gui/$(id -u)/$label"`, and remove that queue's plist. Chain the bootout on
`qk queue status --idle`, which exits 0 only when no worker runs and no entry is queued, testing or
passed, and 75 otherwise; plain `qk queue status` exits 0 for a queue in the middle of a gate too
([#21](https://github.com/vocaro/queuekeeper/issues/21)). The bootout ends a running worker and its
gate, as it did to another session's on 2026-11-12, when a bootout chained on plain
`qk queue status` followed its `worker: running`. A worker between rounds with nothing waiting is
still a running worker, so `--idle` does not call it idle. An entry queued after an idle reading
would start a worker the bootout then ends, so `qk queue close` comes first
([#32](https://github.com/vocaro/queuekeeper/issues/32)), and `qk queue reopen` after, whether or
not the removal went through, but only when this `close` was the one that closed the queue. A held
queue with entries waiting never becomes idle: resume it, or remove its entries, before waiting.

```sh
python3 scripts/qk queue close --why "removing the queue's LaunchAgent" && {
  until python3 scripts/qk queue status --idle; do sleep 30; done &&
    /bin/launchctl bootout "gui/$(id -u)/$label" &&
    rm "$HOME/Library/LaunchAgents/$label.plist"
  python3 scripts/qk queue reopen
}
```

## The README's screenshots

The README's pictures of the dashboard are taken from the dashboard itself, drawn from made-up data. Retake them
after a change to how the dashboard looks:

```sh
python3 tools/dashboard-screenshots.py
```

It writes `assets/dashboard-home.svg` (the Queue home view) and `dashboard-farm.png`, with
compact sample data in the dark theme, and needs Google Chrome. `dashboard-home.svg` holds the home view's picture
with its Flow map laid over the picture's own, as the page drew it, with the stylesheet's Flow rules written in: the
map's dots move by SVG's own animation, which a README image plays, and stand still where the viewer asks for
reduced motion. The made-up data is in the script.

Run it directly on this Mac, as shown, not through `qk admit`. The rule that every test or build takes
admission does not cover it: it starts one headless Chrome for a few seconds, it needs this Mac's Chrome,
and it writes into this checkout. `qk admit` may send it to another Mac, which may have no Chrome and would
keep the pictures in its own checkout, so there the script refuses with exit 2
([#172](https://github.com/vocaro/queuekeeper/issues/172)). Its test still runs through admission, as
every test does. `tests/visibility/test_dashboard_screenshots.py` fails when a panel's data
no longer has the fields the script makes up, which is when to change the script.

## Spelling check

This check is Queuekeeper's legacy spelling lane, which a later release removes: a project lists a
spelling check of its own as a preflight lane instead
([Preflight lanes](../user_guide.md#preflight-lanes),
[#69](https://github.com/vocaro/queuekeeper/issues/69)).

Queuekeeper's own repository already does: its `spelling` lane, the one entry of its
`queue.preflight`, runs `python3 scripts/check_spelling.py [PATHS]`, a standalone copy of this check
that imports nothing from the package, so it outlives the built-in one. This project's exemptions
are that script's `PROJECT`, in place of queuekeeper.json's `spelling`, and it reads the subcommand
names from `python3 scripts/qk -h`. Until the built-in check is removed, a change to its rules is
made in both. The rest of this section describes both.

`python3 scripts/qk spelling [PATHS]` finds British spellings anywhere in a project's text,
identifiers included, and in its file and directory names, and exits 1 on any. With no paths it
reads every tracked file, which is what a push carries; named paths also read untracked files
Git does not ignore, so a page is checked before it is published from disk. Gate pushes on it:
the words arrive one at a time and are invisible in review.

It splits each run of letters at camelCase, snake_case and SCREAMING_CASE boundaries and judges
each subword whole, by pattern (-ise, -yse, -our, -tre, -bre and doubled-l inflections) and by
an irregular list, with a list of American words that only look British. A pattern counts only
the letters British words put before its ending, so a name, a foreign word or a run of text that
lost its spaces whose fix would not be a word passes: Louise, Apryse, bonjour, notre, septiembre,
milesperhour. A string escape such as `\n` or JSON's `\u201c` ends a word. Every file name is
judged the same way, binary files included, split also at `/`, `.`, `-` and `_`, because tests,
docs and scripts refer to a file by name and each reference must move when it is renamed. A hit
reads `PATH: British spelling in file name 'WORD' -> AMERICAN`, and a directory is judged once,
as `DIR/`. Its positive controls, for words and for file names, run before any file is read, so
a detector that stopped detecting fails instead of passing as a clean tree. Exit 2 means nothing
was checked. The rules and what each exemption was learned from are in
`src/python/queuekeeper/spelling.py`.

It already exempts other projects' British-spelled API names, such as Apple's `isCanceled` and
Unity's `MonoBehaviour`, URLs, a list item's first `<em>` (a cited title) and a line marked
`spelling: keep` with a reason, such as a decoder reading a name stored before a rename. A
project lists the rest in queuekeeper.json's `spelling`, each mapped to its reason: `skip` for
paths of recorded observations or retained upstream text (a key ending in `/` is a directory),
`exemptPhrases` for a standard's terms, `exemptRuns` for whole identifiers another project
named, and `exemptTokens` for proper names and foreign words; file names are held to the same
exemptions as text. A
skipped path keeps its own name as well as its contents, since a recording or upstream text
keeps the name it was given, and the skip key's text is exempt wherever it appears, so its entry
and links to it spell the kept name; the directories above it are still judged. A bare case of
another project's type cannot be
told from a project's own name, so mark it: `case .canceled:  // spelling: keep, URLError.Code`.
Fix a hit by spelling the word the American way; never widen a pattern to make one go away.
An American word that a pattern mistakes for British, such as valise or hatred, is no project's
exemption: it joins the checker's American words, with a sample in its controls. Proper names
and foreign words whose endings British words share stay with each project, in `exemptTokens`,
because the project knows why its fixtures and corpora hold them; the toolkit ships no shared
list of them.

The same check fails text that calls the command by its old name (vocaro/queuekeeper#35). The
command has been `qk` since 2026-11-12, and nothing else stops a branch written before then, or a
wrapped line a rename misses, from telling a reader to type the package's name. A hit is that
name, lowercase and a word of its own, followed by a subcommand, and reads
`PATH:LINE: the command's old name 'OLD COMMAND' -> qk COMMAND`. The subcommand names are those of
the command line's own parser as that run built it, a loaded extension's and an enabled visibility
feature's included, so a new subcommand is covered when it is added. The check reads through what
a wrapped line puts between the two: spaces and line breaks, a string's `\n`, the quote closing
one string literal and the quote opening the next, a backslash that continues a line, and a
comment's leader at the start of the next line. What keeps the name is no hit: the capitalized
product, a path or file name such as `queuekeeper.json`, the `queuekeeper:` that begins an error,
and the prefix of a line a subcommand prints, which is its name and then a colon, as in the
`queuekeeper unittest: N tests ...` summary the merge queue parses. Anything else kept on purpose
is marked `spelling: keep` with its reason or listed in `exemptPhrases`. Fix a hit by writing `qk`.

## Docs check

`python3 scripts/qk docs check [PATHS]` checks a project's Markdown: each link's file, anchor and
source line (`PATH:LINE`) must exist, and an `AGENTS.md` must stay under 32 KiB. With no paths it
reads the Markdown at the root and under `doc/`, `docs/`, `guides/`, `knowledge/`, `agents/` and
`policies/`.

**Commands a reader types.** Each `qk WORD` in a fenced shell block (`sh`, `bash`, `zsh`, `shell`,
`console` or none) or in a code span, by any path to the command and with `--project` before the word
if given, must name a subcommand: one of the core's, the project's extensions', or any plugin's or
visibility feature's in this checkout, whether or not it is loaded or on. A hit reads `PATH:LINE:
names no qk command: qk WORD`. A word in capitals, as in `qk COMMAND`, is a placeholder and not read.
The docs once told the owner to type commands that no shell found, and nothing ran them as typed
(vocaro/queuekeeper#39); a test of the setup script likewise runs the `qk --help` it installs, found
on the PATH by name.

**Citations, for a project that turns them on.** With `"docs": {"citations": true}` in
queuekeeper.json, the check also reads citations of a document by its path and a section title,
as in `(doc/guides/shared-host.md, Heavy-work admission)`, and by its path and an anchor, as in
`doc/guides/shared-host.md#probing-stop-signals` (vocaro/queuekeeper#114). Queuekeeper turns it on; a
project without the key gets the Markdown checks alone. Code comments, messages and schema
descriptions cite the guides that way, so with no paths it reads every tracked Markdown file,
source file, script (an extensionless file that starts `#!`) and `*.schema.json`, and with paths
those of the files named. It reads no other data, logs or plain text, and nothing under an
`evidence/` or `archive/` folder, which records what a guide said when it was written. The path is
from the checkout's root or from the citing file's folder. The title must begin with a heading of that document, or with a bold lead-in
that opens one of its paragraphs or list items, such as **Landing ahead of the queue.** A citation
may go on past the title, as in `Two gates and Checkpoints`, and may wrap across the lines of a
comment or across adjacent string literals. A hit reads `PATH:LINE: cites a missing document`, `cites
a missing anchor` or `cites no heading or bold lead-in of`, and the fix is the citation, or a
heading that a rename lost. Text after the comma that begins in lower case, names another
document, or holds braces, brackets, operators or a call before it closes is a list, prose or
code, not a citation. A citation of another repository's document, marked by the possessive before
its path as in "the toolkit's doc/guides/releasing.md, Keeping the split", is not read, since this
checkout cannot resolve it.

It is each project's choice because the projects' records and code quote guides their checkouts
cannot resolve. On 2026-11-13, read in every project, it failed in nine: 395 hits in BreezEPUB
alone, mostly lane logs and archived instructions citing the toolkit's guides without a
possessive, and JSON data in PDFReflowLib read as citations. Off, each project's check fails
exactly as it did before. With data, logs and records left unread, the same projects had 67 hits,
25 of them mpm's placeholders and citations of its own `doc/phase_log.md` from code by its bare
name; a project fixes those before it turns the check on.

When guide sections moved here from the toolkit (vocaro/vocaro-engineering#403), citations of
them went stale with nothing reporting it, and a one-off script found them. On 2026-11-13 this
check found two more in Queuekeeper: one in `doc/guides/shared-host.md` that named Reserved simulator
pool in `simulator-testing.md`, a guide not in this checkout, for a section now in
`simulator-pool.md`, and one in `qk run --help` that named `A run with no verdict`, a bold lead-in
cut short. The whole check of this checkout, 239 tracked files, took 0.8 s that day, well inside
the docs lane's limit.
