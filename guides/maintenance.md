# Maintenance

These mechanisms and lessons apply when the stated capability is present. Original
measurements establish their recorded platform and workload; they do not establish
universal behavior on later operating systems or in a different application.

## Shared-machine operating rules

The rules themselves are in the [common rules](../agents/core.md) and the
[current host policy](../policies/shared-host.md); this section adds how to apply them. Actual
host inventory and authorizations stay outside the generic rules.

- Rules change, and a worktree keeps the copy of each instruction file its branch was cut with.
  Before a step that uses something every session shares (a device, either Mac's screen), run `python3 scripts/eng policy refresh` and read the project's rule as it
  stands on `main`: `git show origin/main:AGENTS.md`.
- A task handed over by another session is that session's request, not the owner's. Where a rule
  says "unless the owner asks", ask the owner.
- Own what you start and stop only that: processes, simulators, devices, settings and files.
  Preserve every other session's work, including a run that looks idle.
- Give every simulator and device command the canonical uppercase UDID of a device you own or
  have reserved; never select by name or `booted`.
- Take no foreground input or Mac VoiceOver session on this Mac without a period the owner
  approved beforehand, and none on the second Mac outside the foreground eng holds for a run;
  launch no debugger. Do not open a live simulator view or use any tool that
  controls a simulator's screen, even for a screenshot or a tap, unless the owner asked to
  watch: such a tool asks the owner to approve each device on its first use, and a new pool
  member is new to it.
- Before proposing a physical device, say what it would show that a simulator cannot. A check
  said to need a finger usually needs a timing or an input that a UI test can make.
- Record what automation cannot reach as not verified, and leave it to the owner.

[Shared host](shared-host.md) holds the mechanisms: admission, reservations, supervision and
inspection without a debugger.

## Concurrent Git integration

Every session's worktree shares one `.git`, and `main` gains commits many times a day.

**Name the base commit.** Another session's `git fetch` moves `origin/main` in your worktree. To
squash, rebase or compare, read the commit your branch is based on once, with
`git merge-base HEAD origin/main`, and use that commit rather than the ref. A
`git reset --soft origin/main` made after the ref had moved staged main's new commits as
deletions. A patch made with `git diff origin/main -- FILES` to take a session's own change out
of a tree held another pull request's changes as well, and the tree a test then ran on was
neither main's nor the branch's. To take your own change out, reverse the patch of your own
commit, and check that the diff against the base is empty before calling the tree main's.

**Check what a brief says against what is there now.** A pull request a brief names as open may
have merged since the brief was written; look before building on its branch, and once it has
merged, build on `main`. A test merged since your branch was cut may pin a decision the owner
has since reversed; hold it against the decision recorded on the issue before changing the test
or your code. A condition in a brief, such as "if the pull request is still open", is read again
straight before the step that depends on it, not only at the start: a pull request can merge
within a minute. Before pushing to a pull request's branch, run `gh pr view N --json state` alone,
in its own call, and push in a later call only if it says `OPEN`; a compound command that prints
the state goes on regardless. A remote-tracking ref is no evidence that a branch exists, since
`git fetch` without `--prune` keeps it after GitHub deletes the branch; `git ls-remote --heads
origin BRANCH` is the check. A push to a branch GitHub deleted at a merge re-creates it, and a
session may not be allowed to delete it again. On 2026-10-13 a brief's push went to a pull
request merged 28 seconds after the session read it, and the owner had to delete the re-created
branch.

**Overlapping sessions.** On 2026-10-12 two sessions, each started by a different session, made
the same fix within minutes: neither brief named the other, a search of issues and pull requests
showed nothing because neither had one yet, and messages between them stayed queued for hours.
What gave it away was the other session's title in the app's list of linked sessions. So:

- Before the first change on a handed-over task, and again before committing, read the linked
  sessions' titles and, for one that overlaps, its worktree's log and status. A clean worktree
  says only that nothing is written yet; ask its session or the owner, and do not wait for an answer.
- When two branches hold the same change, the later session merges the earlier one's pushed
  branch into its own, takes its wording where they agree and lands only what it adds. It does
  not push to the other session's branch.
- A brief names the sessions its writer knows to be working near the same code. A session that
  will file an issue for a task it hands over says so in the brief, or files the issue first and
  names it. Search the tracker again straight before filing an issue: on 2026-10-13 a started
  session filed a duplicate three minutes after its starter filed the original.
- Messages cannot hold a working session back, since it reads them when its turn ends. Sessions
  that must not build or commit at the same moment work in a worktree each. Where two share one
  checkout, each says before it starts which files it will edit, commits only those, and checks
  `git status` and its head against the remote before every commit; a hold the other must
  respect is something it meets in the tree, such as a file it checks before building or
  committing.
- Hand out parallel work by files, not areas. Before giving out issues, list the files of every
  open pull request (`gh pr list --state open --json number,files`) and of every branch waiting
  in the merge queue, choose issues whose code is in none of them, and give each session that
  list, every sibling's issues and files, and, where two must edit one file, the functions each
  keeps to. On 2026-10-22 twelve agents made 14 pull requests this way with no conflict in a
  source file. This finds only work already on a branch, not a session that has written nothing
  yet; the linked sessions' titles cover that.

**Fetch before the last verification, and do not chase `main`.** When `main` moves faster than a
full verification runs, verifying again after each rebase never ends. On a direct-main repository
the [merge queue](#the-merge-queue) ends that: a session runs its smallest relevant checks and
lands with `eng land`. On a pull-request repository, open the pull request once the tests pass on
a commit that holds `main`, merge `main` again when it conflicts, and say which commit each run
used.

**Keep evidence off branches.** What a probe or spike found goes on the issue or in the subject
document it bears on. Code worth having again goes on the issue as a patch, with the commit it
applies to. "Still on the remote" is not a way to find something.

**Verify the integrated tree.** Two changes that each pass can fail together. Cherry-pick each
finished change onto the branch that will be pushed and run the gate there, not in the
worktrees. When several branches are ready at once, one gate can serve them: merge them onto
`main` in a local branch that is not pushed, keep each branch's changelog line, run the gate
there, and say on each pull request which commit ran and which heads it held. A filtered run on
one platform does not stand in for another platform's suites. On a direct-main repository,
cherry-pick one commit per finished branch onto `main` in that local branch instead, and give
each commit one closing line per issue it closes, adding any its branch left out. A conflict that
is a design merge rather than text, such as one branch moving a file another changed, goes back
to the branch's author, who rebases onto the integration branch's head and knows why each change
was made.

**Commit handed-over patches before integrating them.** An agent told to leave its work
uncommitted hands over one patch per issue, made against the base commit, with the order it
applies in. In a worktree at that base, `git apply --index PATCH` and `git commit` each in
order, then cherry-pick the commits. A cherry-pick leaves the base, branch and picked versions
of every conflicted file in the index for a three-way merge and carries the change's message and
its closing line; `git apply` refuses a patch whole where the branch has changed the same lines.
Shared records (generated counts, allow tables, issue-state snapshots) conflict in the same
places on every pick: regenerate them from their sources after each pick rather than merging by
hand ([documentation checks](#documentation-checks)).

**Read a project's hooks before checking it out somewhere new.** A worktree of another project
runs that project's hooks. mpm's `post-checkout` opens the Unity editor for each new worktree, on
the owner's screen (2026-10-28). Read `git config core.hooksPath` and the hooks it names before
`git worktree add`. When a checkout hook would launch anything, add the worktree with
`git -c core.hooksPath=/dev/null worktree add …`, and then commit and push under the project's
hooks as usual. A plain `git checkout` in such a worktree can fire the same hook, as mpm's did
on 2026-11-01 in a worktree added that way, so pass the same option there. A worktree outside `~/Development` also loses the toolkit's sibling path: run
`scripts/eng` there with `VOCARO_ENGINEERING_ROOT` set. A project file may hard-code that path
too. studywright's `app/project.yml` names `../../vocaro-engineering`, so its `check-app.sh`, which
its pre-push hook runs, fails from a worktree that has no toolkit checkout beside it.

**Push only onto the `main` you tested.** The merge queue does this for every commit it lands.
Immediately before pushing to a direct-main repository by hand:

```sh
git fetch origin
git merge-base --is-ancestor origin/main HEAD && git push origin HEAD:main
```

Never pipe the push, or any step whose failure must stop what follows, through a filter such as
`tail` or `grep` in an `&&` chain without `set -o pipefail`. A pipeline's status is its last
command's: a failed push piped through `tail` reads as success, its error is cut, and the chain
goes on to what needed the push, such as a release of a commit `main` does not have.

If `origin/main` is not an ancestor, rebase onto it. A tool that brings `main` in by merging,
such as the Claude app's sync with the base branch, leaves a merge commit that the push would
put on `main`; rebase the branch's own commits instead. When `main`'s new commits change only
documents the branch does not touch, or only files that the project's `doc/testing.md` lists as
read by none of the gate's suites, the gated result carries over: check that the rebased tree
differs from the gated one by exactly those commits, run the documentation check and the lane
the list names for each changed file, and push. Otherwise run the gate again on the rebased
tree, and push that. A carried-over result is not a gate of the pushed commit: a release is
tagged only on a commit its gate passed.

```sh
cmp <(git diff GATED HEAD) <(git diff OLD_BASE origin/main)  # silent: the same change
```

Never force-push `main`. Every repository declares its route in `engineering.json`'s
`integration` (`direct-main` or `pull-request`), and `eng doctor` warns when one does not.

**An issue is closed by what `main`'s tree holds.** Not by a commit somewhere that names it, not
by a branch that would fix it, and not by a merge that records such a branch as merged.

- A commit says `Closes #N` only if it is meant to land on `main` with its content, and only
  when the person landing it is entitled to close that issue. Work in flight says `Refs #N`. The
  closing commit folds the facts that still describe the product into the owning document.
- A merge whose tree equals its first parent's tree brings no content, whatever its parents say.
  Every commit it introduces is recorded as merged and absent from the tree, and an issue whose
  every `Closes` sits in that set is closed by nothing.
- An abandoned branch's issues are reopened, or left closed only with a comment naming the
  commit that holds the unported fix, so the next person learns it before starting.
- GitHub ties a closing keyword to the one issue number straight after it, and reads a closing
  verb before a number as a keyword whatever comes before the verb. "Fixes #A and #B" closes
  only #A, and "does not close #N" closes #N. Give each issue its own `Closes #N.` line, and
  write "leaves #N open" for one that stays open.
- An issue left open after its change lands, until something is seen in real use, says what
  closes it and how to check: the observation, and the command or path that shows it. Nothing
  else brings anyone back to look, so a session that later claims, comments on or closes out
  work on that issue runs the check first, and closes the issue with the result when it passes.
  When the observation is near, such as the next routine gate, the landing session waits for it
  instead. [#218](https://github.com/vocaro/vocaro-engineering/issues/218) stayed open on "until
  a real gate on the second Mac shows the files" after the first such gate had written them, two
  hours after landing, through two later comments by sessions that did not look (2026-11-07).

A closing-commit check holds this mechanically from Git alone, offline: it finds each empty
merge, fails one that strands a closure claim unless a recorded reconciliation names it, notes
one that strands nothing, and passes an issue also claimed by a commit the tree holds. It
refuses a shallow clone, which cannot show whether a merge brought content, and it cannot see an
issue closed by hand on GitHub against a branch commit. Closeout's `retire.sh` makes an empty merge
on purpose, to keep in `main`'s history the commits of a landed branch that evidence cites. It
refuses one that would strand a claim, or close an open issue ([agent skills](skills.md)).

GitHub closes an issue when a `Closes` commit reaches `main` on the server, so fixes committed
to a local `main` leave their issues open and looking untouched. Before choosing work, list the
open issues a local commit already closes; each number printed is work to gate and push, not to
redo:

```sh
comm -12 <(git log --format=%B main | grep -oE 'Closes #[0-9]+' | grep -oE '[0-9]+' | sort -u) \
  <(gh issue list --state open --limit 200 --json number --jq '.[].number' | sort)
```

Read an issue's body before its comments. `gh issue view N` prints the title and body, but with
`--comments` and its output not a terminal, as an agent's never is, it prints only the comments
(gh 2.102.0). A session that read an issue that way once closed it having done only what a
comment added (2026-10-27).

## The merge queue

Six of the owner's repositories landed 8 to 14 times a day in the two weeks to 2026-10-31, so a
45-minute gate that restarts whenever `main` moves is usually beaten by the next landing: in the
busiest, 82% of landings were followed by another within 45 minutes. The queue makes one process
per repository the route to `main`, as GitHub's merge queue does, on the owner's Macs:

```sh
python3 scripts/eng land --wait                 # HEAD, as a background command: 0 landed, 1 came back, 75 held or still waiting
python3 scripts/eng land BRANCH                 # returns at once, and the commit lands by itself
python3 scripts/eng land --repo EPUBLib BRANCH  # from any project: land a fix in another repository
python3 scripts/eng queue status
```

**A gated push keeps the app.** For a project whose engineering.json has `app`, the worker starts
`eng app keep --after-landing` for the pushed commit in the background once the push lands, and the
round goes on without waiting. That build is what `eng app run` opens ([kept app builds](app-builds.md)).

**A session does not run the full gate.** It runs the smallest relevant checks, commits and adds
the commit. Adding is the decision to land: the owner approves that once, and no later step asks.
What is added is the range the ref has and `origin/main` lacks, held by a ref of its own, so the
session may end and its branch may move or go. Refreshing a branch by merging `main`, by the
app's sync or `git merge origin/main`, is fine: when each merge in the range brings in only commits
`origin/main` holds, with the tree a clean merge makes, `eng land` replays the branch's own commits
onto `origin/main` in a scratch worktree, queues that, says which merge it dropped, and leaves the
branch as it is, whose work the landing check then finds as equal patches. Any other merge, or a
replay that conflicts, is refused (vocaro-engineering#217). Adding a branch again replaces what it
added before.

**A fix in another repository lands there the same way.** Commit it in a worktree of that
repository, then `eng land --repo NAME BRANCH --wait`, from wherever the session is. It joins that
repository's queue beside its own sessions' commits and so stops none of their work. A commit must
be named, because HEAD there is another session's. `--wait` prints `landed: COMMIT` in full: the
entry's own last commit on `main`, not another entry's at the batch's tip, and its status line
above says `-> COMMIT (N commits), in batch tip TIP` when the entry has several commits or another
entry's commit tops the batch. Cite that commit. Move the dependent project's pin to it in a
commit of its own, and land that in the dependent's queue.

**A round.** The worker fetches, takes everything waiting, oldest first, cherry-picks each entry
onto `origin/main` in its own checkout, `~/Development/vocaro-queue/NAME/NAME`, and runs the project's queue lanes through
`eng run`: its `spelling` and `provision` lanes when it has them, and then its `gate` lane, or
`queue.lanes` in engineering.json for a project that needs others, read from `main`, or from the
batch until `main` has any, so that the commit naming them can land. A project with no `spelling`
lane gets the toolkit's: the worker runs `python3 scripts/eng spelling` in its checkout, on this Mac,
before anything else, so that a British spelling fails in about a minute instead of after a full
gate and a bisect, in every project without each declaring the same lane. A project's own
`spelling` lane runs in its place, and `"queue": {"spelling": false}` on `main` turns the toolkit's
off for a project that cannot pass it yet; the round's log says when the toolkit's ran. The
lanes therefore go where every lane goes, to whichever Mac has spare capacity for them, the second Mac
first ([which Mac runs a lane](shared-host.md#which-mac-runs-a-lane)), and the round's log says where
each ran and why; one gate serves the whole batch. On a pass it pushes
exactly the commits it tested, as ordinary commits with their own messages and closing lines, so
Closeout finds each branch landed by equal patches. The gate receipt is of the pushed commit, which
`eng release` accepts, unless the result was carried over. Each landed entry records its gate's wall
time with the CPU busy share of the Mac the gate ran on, read every minute during the run, so that
the gate's budget median can leave out runs made on a loaded host
([keeping the split](releasing.md#keeping-the-split), [#235](https://github.com/vocaro/vocaro-engineering/issues/235)).

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
above all. Each settled commit is listed under `kept` in the entry's record, which `eng queue status`
and `eng queue wait` show. The commit keeps its message and author. Its patch is then not the
branch's, so `landed.sh` proves the branch landed another way, `resolved`: each branch commit has a
commit on main with its subject and author that adds and removes exactly its lines, file for file
and hunk by hunk, and differs only in the lines around them. It compares each commit with the one
it landed as, so a line later work on main rewrote still counts. Closeout and its sweep then delete
the branch like any other landed one (the owner's decision of 2026-11-04,
[#164](https://github.com/vocaro/vocaro-engineering/issues/164)). A resolution that dropped or
changed one of the branch's lines proves nothing, and that branch stays for its author.

**Which toolkit a gate runs.** Each round, a project's worker moves
`~/Development/vocaro-queue/NAME/vocaro-engineering`, a locked worktree of the toolkit with no session's
edits, to the toolkit's `origin/main`, and runs the lanes, the pre-push hook and the carry-over checks
with `VOCARO_ENGINEERING_ROOT` naming it (owner's decision, 2026-11-01). A landed toolkit commit
therefore reaches the next round of every project's gate, and a shared toolkit checkout that lags or
holds a session's changes reaches none of them. Each worker has its own copy, so moving it changes no
other project's running gate. The worker follows its copy: once the copy is at `origin/main`, a worker
whose own code came from another commit starts itself again from the copy, with the same arguments and
environment and the bisect it had reached, before the round touches any entry, and its log says
`moving to the toolkit at COMMIT, origin/main, from COMMIT, which this worker was started from`. So what
judges `main`, its `engineering.json` above all, is the toolkit the lanes run, not the older code of the
checkout the worker was started from. On 2026-11-04 EPUBLib's worker, started before
[#168](https://github.com/vocaro/vocaro-engineering/issues/168) added the lane key `wholeHost`, landed
a commit whose gate lane set it and then held the queue on `Invalid lane gate`, as its own code refused
the key its copy had accepted, until a person resumed it ([#173](https://github.com/vocaro/vocaro-engineering/issues/173)). The toolkit's own queue's lanes test its
batch instead, but its worker moves the same way, to a copy of its own that no lane reads: a running
worker keeps the modules it loaded, so one started from the shared checkout, or a session's worktree,
would judge the next round's `main` with that code even after the shared checkout followed a landing,
and a toolkit commit that added a key and set it in the toolkit's own `engineering.json` held the queue
once ([#177](https://github.com/vocaro/vocaro-engineering/issues/177)). Since every toolkit landing
moves `origin/main`, that worker starts itself again in the round after each one. A
`VOCARO_ENGINEERING_ROOT` set where the worker started stands and does not move it.

A worker does not move to a toolkit that could leave the queue with no worker to land its fix. Before a
move it imports the worker's code from that commit's files in a process of its own, and the queue's
directory keeps the commit a worker moved to, `moved`, until the worker started there finishes a round.
A commit that does not import, or one a moved worker died or held in before finishing a round, is not
moved to: the worker goes on with its own code, a project's lanes with the toolkit it was started from,
and its log says `staying on the toolkit at COMMIT, which this worker was started from, rather than
moving to COMMIT, origin/main:` and why. So a broken toolkit pushed to `main` by hand is fixed through
the toolkit's queue as any commit is, and the next commit on `main` is moved to again. A copy that cannot be fetched is a remote out of reach, tried again as any is, and one that
cannot be moved holds the queue for a session; neither blames a commit. Anything else still
reads the shared checkout: a session's own `eng`. So a project commit that needs a new toolkit option lands after the toolkit's commit, and runs
by hand only once that checkout has it.

**A worker whose session ended exits.** macOS denies a worker the local network once the Claude session that
started it ends, so it could no longer reach the second Mac. Before each round, while a second Mac is configured, the
worker checks for this and, if so, exits, holding nothing, with its entries back in the queue. Its log then says
`exits, holding nothing, since this worker cannot reach the second Mac`. The next `eng land`, `eng queue wait` or `eng
queue resume` starts a worker from a live session (guides/shared-host.md, Local Network access;
vocaro-engineering#282).

**The shared checkout follows each toolkit landing.** After each push, the toolkit's own worker
fast-forwards its main checkout, `~/Development/vocaro-engineering`, to `origin/main`, without hooks,
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
from it, such as `python3 scripts/eng`, names a file below it
([vocaro-engineering#182](https://github.com/vocaro/vocaro-engineering/issues/182)).
A running `eng` keeps the modules it loaded at its start but reads six others when first used, and a
lane that starts another `eng` reads the tree as it is then, so a run spanning the move could mix two
toolkits. Otherwise the worker leaves the checkout as it is and its log says why, naming up to five
of the processes and what of each it found, such as
`left ~/Development/vocaro-engineering as it is: it is on BRANCH, not main`; nothing of this holds
the queue, and the next landing tries again. A checkout left only for such a run, or for a census that
failed, the worker also tries again once nothing waits, every 30 seconds for up to an hour, ending the
tries as soon as an entry arrives, and says when it gives up; one left for any other reason is a
person's to settle. A worker whose code came from that checkout, the
toolkit's own among them, is such a process until its first round after a toolkit landing, when it
starts itself again from its copy, above. A process that loaded the toolkit from it without keeping a path there,
as a project's launcher does, cannot be seen, and a process that starts between the check and the
fast-forward is not either. A session never stashes, resets or edits that checkout by hand to make it
follow.

**Where the queue's checkouts are.** Each queue's pair is in one folder beside the main checkouts and
outside every repository, laid out as the second Mac lays out a run's (`checkouts/NAME/N/...`):

```
~/Development/vocaro-queue/NAME/NAME                 the worker's checkout of the project
~/Development/vocaro-queue/NAME/vocaro-engineering   its copy of the toolkit
```

So a project file that names the toolkit by a path relative to the checkout, as Pagepour's and
studywright's `../vocaro-engineering/src/swift/EngineeringTestSupport` do, finds the queue's own copy,
on this Mac as on the second Mac, with no change to the project. The toolkit's own queue is
`vocaro-queue/vocaro-engineering/vocaro-engineering`, with the copy its worker runs from beside it,
`vocaro-queue/vocaro-engineering/vocaro-engineering-toolkit`. A folder inside the
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
and says so in its log, and `eng disk` lists it for the owner's removal once nothing uses it.

**A conflict is found when the entry is queued.** `eng land` and `eng queue add` make the
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
see, below, and an entry ahead that comes back may let a refused entry apply, which `eng land` again
checks. A check that cannot be made, such as a worktree Git cannot add, queues the entry, says so, and
leaves any conflict to the round. It costs a worktree checkout and the cherry-picks, measured on
2026-11-04 on this Mac at a one-minute load of 28 to 45 with six one-commit entries ahead: about
0.6 s with none ahead and 1.6 s with six on the toolkit's tree of 203 files, and 4.5 s and 6 s on a
tree of 10,000 files, where each cherry-pick adds about 0.3 s. Whole `eng land` runs, fetch included,
took 1.5 to 2.7 s and 3 to 9 s.

**What comes back to the author.** `eng queue status` and `eng queue wait` name each entry's state,
reason and log. An entry that conflicts with what is ahead of it, which the check could not see
when it was queued, comes back at once and costs the others nothing. Its reason names the files in
conflict and what it conflicts with: `origin/main` when no entry ahead changes those files; only the
entries ahead that change them, by ID, when `origin/main` still holds those files as the commit's parent
did; and otherwise `origin/main` and every entry ahead
([#247](https://github.com/vocaro/vocaro-engineering/issues/247)). Those entries may still be in their
gate: once they land, rebase onto `origin/main` and add it again. A session therefore
lands with `eng land --wait` run as a background command, whose end wakes it within seconds of a
send-back; on 2026-11-03 a session that had run plain `eng land` learned of a conflict only half an
hour later, from `eng queue status`
([#130](https://github.com/vocaro/vocaro-engineering/issues/130)). When a batch fails, the oldest half runs next, and so on until one entry fails
alone; every entry that passes along the way lands.

**A branch that goes on after a landing.** The queue lands a branch's commits under new IDs, so a
branch that gets more work after one of its landings still holds the landed commits under their old
IDs. `eng land` replays them onto `origin/main`, and one whose lines a later landed commit changed
conflicts there, so the branch is refused as a merge it cannot replay (2026-11-07, #204). Start the
next change from `origin/main`, or `git rebase origin/main`: git drops a commit whose patch is already
on main, and `git rebase --skip` passes one it stops at whose change main holds in a later form.

**A bisect's two halves gate at once.** While a bisect goes on, the newer half of its suspects gates
beside the older half, in a second checkout, `~/Development/vocaro-queue/NAME/NAME-beside`, on the same
`origin/main` (owner's decision, 2026-11-07, [#241](https://github.com/vocaro/vocaro-engineering/issues/241)).
The older half runs on this Mac (`eng run --here`) and the newer on the second Mac
(`eng run --offload-only`), which runs it only if it can start it now. Whichever half passes lands,
so a newer half that passes can land before the older one. When both pass, the older half lands first,
and the newer one lands on top of it only if its result carries over; otherwise it gates again. When
both halves fail, the older one is bisected first. The newer half instead waits behind the older one,
as before, when:

- the second Mac declines it, or gives no verdict;
- a lane runs only on this Mac (a `self` lane, or one that says `here`; a light lane is offered as
  every lane is since 2026-11-07);
- the round is docs-only, ungated or `--front`, or a newer entry was added with an option; or
- a newer entry applies to `origin/main` only on top of an older entry.

Under #231 the rerun-alone check runs on each failed half, so the failure that splits a half is a
real one.

**Entries that arrive during a gate gate on top of it.** While a batch gates, the worker checks every
`STACK_POLL` (30 s) for entries that have arrived behind it. When it finds some, it gates them stacked on
the batch's tip, in the checkout the batch does not use, on the other Mac: on this Mac when the batch went
to the second Mac, and otherwise on the second Mac if it can start them now (owner's decision, 2026-11-07,
[#241](https://github.com/vocaro/vocaro-engineering/issues/241)). The batch's push makes that tip
`origin/main`, so the next round adopts the stacked gate as its own: it gates on, and its result settles
it as any round's would, with no gate of its own to start. Anything else throws the stacked gate away,
canceling its lanes and queuing its entries again:

- the batch fails or gives no verdict;
- the batch lands at another commit (after a carry-over, or with a `Flaky-test` trailer);
- the stacked gate gives no verdict, as when the second Mac declines it;
- an entry of it is removed;
- an entry goes first (`--front`, `--no-gate`, or a pass waiting for its push);
- main's lanes change;
- the queue holds; or
- the worker stops or moves to a newer toolkit.

Only one stacked gate runs at a time, so entries that arrive after it starts wait for the next round, where
they stack on it in turn. A stacked gate settles nothing itself: an entry that conflicts with the batch below
waits for its own round, and the worker's log names the entries below that it conflicts with. A bisect stacks
nothing, as both its halves already use both Macs. An entry that comes back keeps its ref until
`eng queue remove ID`. Fix it, rebase and land it again.

**Removing an entry that is testing stops its gate.** `eng queue remove ID`, or `eng land` of the branch
again, takes the entry out of the queue at once. The worker looks every `REMOVAL_POLL` (5 s) for an entry
of the gate it runs that has left the queue, and then stops that gate's lanes as it does a stacked gate's
it throws away, on this Mac or the second, which ends the run there and frees its admission. It gates what
is left of the batch again, with no verdict counted and no hold, and stacks nothing on a batch an entry of
which has left, since that tip will not land (owner's request,
[#247](https://github.com/vocaro/vocaro-engineering/issues/247)). On 2026-11-07 a removed entry's gate on the
second Mac held PDFReflowLib's queue for about 15 more minutes. `eng queue remove` says so of an entry that was
testing; under a worker started from a toolkit older than this, it says instead that the gate runs on until
its lanes end, at most `LANE_LIMIT` a lane, holding the queue until then.

**A failed test that passes alone is a flake.** Before it bisects a batch whose failing lane is
`eng unittest`, the worker reruns the failed tests alone on the same batch commit (owner's decision,
2026-11-07, [#231](https://github.com/vocaro/vocaro-engineering/issues/231)). It reads their IDs from the
lane's log: unittest's `FAIL:` and `ERROR:` lines under each failed process. It first waits for this
Mac, where the reruns run, to settle, as for a retry with no verdict below. Then it runs each test by
itself, with the lane's `-s`, `-p`, `-t`, `--split` and `--timeout`, one process at a time, up to `RERUNS`
times (3). Any rerun that fails, or ends unmeasured, makes the failure real, and the batch bisects as
before. When every rerun of every failed test passes, each test is a flake, and option (b) applies: the
batch lands only if none of its commits changes the test's module or a file of the project that module
imports. Only its own import statements count, at any depth of the module, looked up under the suite's
top-level directory, `src/python`, `src` and the checkout, with each package's `__init__.py`. The files
those imports import in turn do not count. A batch that changes one of them bisects. Either way the
flake is recorded in four places: in each entry's `flakes` (the test ID, the gate's log and the reruns'
logs, which `eng queue status` shows); in the worker's log; in a `Flaky-test:` trailer on a landed tip,
one per test, which survives a carry-over; and on the tracker. For the tracker the worker searches the
project's open issues for the test's name just before it writes, then comments on the first match, or
files a hardening issue. A tracker gh cannot reach is noted in the entry and stops nothing.

The worker bisects at once, as before, when the failing lane is not `eng unittest` (a project's own
lane or a lane group), when its log names no test it can rerun (no `eng unittest` summary, tests also
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
branch of your own once `eng land` says `landed`. In a rebase already under way, `git rebase
--skip` drops a landed commit. On 2026-11-05 one EPUBLib session's rebases stopped on
`CHANGELOG.md` three times this way.

**A run with no verdict blames nobody.** Exit 3 (unmeasured), 75 (no admission) and 78 (a
misconfigured host) say nothing about the commits. The worker runs the lanes again, and holds the
queue, with every entry waiting, once three runs in a row have given no verdict, as it also does for a
push refused for any reason but a race. On 2026-11-04, 8 of 12 of EPUBLib's gates on the second Mac
gave none, as the simulators' voices were not ready in time
([EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946)), and with a single retry each pair held
the queue until a person resumed it. A project sets another bound on `main`, as
`"queue": {"noVerdictRuns": N}`, a positive integer; 1 holds at the first. A run with a verdict, a pass
or a failure, starts the count again, and so does `eng queue resume`. The hold's reason says how many
runs gave no verdict, on which Mac the last ran (`on this Mac (NAME)` or `on the second Mac (HOST)`, when
its receipt or dispatch says), and why it gave none: the lane that reached its limit, the number of tests
its log reports unmeasured with the first of them and the last screen reading eng logged
(vocaro-engineering#185), no admission, or the host's configuration. A hold stays
until whoever its reason names has dealt with its cause, or found it gone, and run `eng queue resume`:
the owner only when the last run's exit was 78, a shared host's configuration, and otherwise a session,
as [a hold says whom it is for](#the-merge-queue) below. Nothing fails over to another route.

Each retry waits first for the host to settle, by the test heavy-work admission applies before it takes
a slot beyond the guaranteed ones (`resources.pressure`; [heavy-work admission](shared-host.md#heavy-work-admission)):
a CPU busy share below `HEAVY_BUSY_LIMIT`, 75% by default, and memory pressure at level 1 (normal); the
one-minute load average judged it until vocaro-engineering#200. A retry that started at once met the load that voided the run it repeated:
on mpm's queue, two gate runs of about an hour each passed every row but voided a timing anchor at loads
of 84 to 222, and the queue held until a manual resume once the load had fallen
([#124](https://github.com/vocaro/vocaro-engineering/issues/124)). The worker looks every 15 seconds
(`SETTLE_POLL`) for up to an hour (`SETTLE_LIMIT`), no longer than the gate run the wait may save. Past
that it holds, with every entry waiting, whatever runs the bound has left, and the hold names the
reason it last read, such as `the CPU is 95% busy, at or above HEAVY_BUSY_LIMIT=75%`. Its log names the
reason when the wait starts, again every ten minutes or when the reason changes, and at the end how long it waited, how many
looks it took and the first, highest and last one-minute load. A stop request (SIGTERM, SIGINT or SIGHUP)
ends the wait at its next look, with the entries still waiting. The test reads the Mac the lane ran
on, since a lane that the second Mac ran left its load there
([#140](https://github.com/vocaro/vocaro-engineering/issues/140)). The worker knows it from the
lane's dispatch, which `eng run` files under `.build/engineering/remote/.dispatches/LANE/` in the
queue's checkout: one that started during the run and reached the second Mac, rather than only
preparing, names it. Each look then asks the second Mac over SSH, as `eng doctor` does, for its
CPU busy share, the `HEAVY_BUSY_LIMIT` it sets there (75% by default), its one-minute load, which the log
records, and its memory pressure, and judges them as admission does; the log and the hold name that Mac. A second
Mac that cannot be reached, or does not answer with its reading, gives this Mac's reading for that look
instead, and the log says so when that starts and again once it answers. A lane that ran here, or that
no Mac ran, reads this Mac only. A lane that fails blames its commits at once, with no wait, and the
hook's no verdict waits the same way before it runs again, reading this Mac, where the worker runs the
hook.

**A hold says whom it is for, and a session resumes it.** Every hold's reason begins with its kind.
`for the owner, since nobody else can fix it` is for a cause only he can fix: a shared host's
configuration (exit 78 past the bound), a push the remote refused, a key or token a remote refused. `for
a session, not the owner` is every other: a host that stayed busy or tests left unmeasured past the
bound, the pre-push hook's refusal or its no verdict, a path where the worker's checkout goes, a `main`
whose `engineering.json` the toolkit at `origin/main` refuses (the reason names the toolkit commit that
judged it), a remote out of reach past the tries below, and anything unexpected. The reason ends with
who runs `eng queue resume`: the session that has dealt with the cause, or any session that finds it
gone, runs it itself and says so, in its report, and never asks the owner for it. The owner is asked
only for a hold that says it is his, and once he has fixed its cause any session that finds the cause
gone resumes (owner's decision, 2026-11-04, [#173](https://github.com/vocaro/vocaro-engineering/issues/173): "why do you need my approval to resume the queue?
this should never need my approval"). `eng queue status`, `eng land --wait` and `eng land` print the
reason with its kind. A hold whose cause a session cannot find is still a session's: say what was
looked at, and what remains, rather than asking the owner to resume.

**A remote out of reach is tried again.** A fetch or push that never reached the remote, or whose
connection ended, says nothing of the commits, and a brief outage clears by itself: a name that did not
resolve, a connection refused, timed out, reset or dropped, over HTTPS or SSH. The worker tries it again
after 10, 30, 90 and 270 seconds (`BACKOFF`); when the fifth try fails too, it runs the round again after
1, 5 and 15 minutes (`CLEARING_WAITS`), staying the worker through the waits, with every entry and a
batch's pass kept, and holds for a session only when the last round's fifth try fails too, about an hour
after the first ([#173](https://github.com/vocaro/vocaro-engineering/issues/173)). A round that went through starts the tries again. So a gate
that passed is not discarded because GitHub could not be reached for a moment
([#113](https://github.com/vocaro/vocaro-engineering/issues/113)). `eng land`'s own fetch retries the
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
log in `eng-queue/logs`, and then pushes with `--no-verify`. A connection that dropped or a race that
leaves the commit unchanged pushes again without running the hook again; a carried-over commit is a
new commit and runs it again. A hook's exit 3, 75 or 78 is no verdict, as a lane's is: it is tried
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

**A gate that fails on a test the change cannot reach.** On an `eng unittest` gate the queue does this
itself when the test passes alone ([a failed test that passes alone is a flake](#the-merge-queue)).
Otherwise, record the failure on that test's issue,
or file one by the project's labels, before adding the entry again: an entry added again with no
record leaves the flake nobody's to fix. On 2026-11-05 the fix for
[EPUBLib#978](https://github.com/vocaro/EPUBLib/issues/978) went through EPUBLib's queue four
times: once on its own regression test's race, then twice on Mac display checks that ran on the
second Mac beside another project's gate
([EPUBLib#875](https://github.com/vocaro/EPUBLib/issues/875),
[EPUBLib#896](https://github.com/vocaro/EPUBLib/issues/896)), each recorded on its issue, while
the iOS lane passed every time. The options below are for a fix that cannot wait, not for a flake.

**Landing ahead of the queue.** Two options of `eng land`, and of `eng queue add`, are for
emergencies only, never routine. They work in every repository, with no setting (owner's decision,
2026-11-04, [#163](https://github.com/vocaro/vocaro-engineering/issues/163)), and do what a hand push
past the queue did, with the tool keeping the record:

```sh
python3 scripts/eng land --front --why "the toolkit fix every queued change needs" --wait
python3 scripts/eng land --no-gate --why "an unrelated flaky test fails the gate" --wait
```

- `--front` tests the entry next, ahead of everything waiting, as a batch of its own. A gate already
  running finishes first, as does a batch that passed and waits for its push; a bisect in progress
  goes on where it was. `eng land --front` again with a commit already queued moves that entry.
  `--why` is optional here, and the entry and `eng queue status` show it. Example: an important
  toolkit fix stuck behind several queued changes jumps the queue. The entries already queued are
  then tested with the fix without being queued again: they rebase onto the new `main`, and other
  projects' workers move to the toolkit at `origin/main` each round.
- `--no-gate` needs a written reason, `--why TEXT`, and is refused without one. It lands the entry
  alone, in its turn: the worker rebases it onto `origin/main`, runs only the project's `spelling`
  lane, or the toolkit's where it has none and has not turned it off, and the pre-push hook, unless `main`'s `queue.hookCoveredBy` says the gate
  covers the hook, and pushes. The reason is recorded everywhere the skip is: the entry's `ungated`,
  with the lanes; the worker's log; the landed tip's `Landed-without-gate:` trailer, its author,
  message and patch otherwise its own; and `eng queue status`. Example: an important fix is held up by
  an unrelated flaky test failing the gate. It is no gate of the pushed commit, so `eng release`
  will not take it. The next gate on `main` covers it: `eng queue status` lists it as `ungated on
  main` until a landing whose gate ran on a base that holds it has passed, and a docs-only landing
  does not count. A session that uses it tells the owner it did and why, as for a hand push.

With both, the entry lands at once, once a gate already running finishes, which is the hand push of
2026-11-04: a one-line change that made a late voice listing a skip on simulators, so that
EPUBLib's gates gave a verdict again ([EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946)).

**Pushing by hand is the exception.** A session pushes to `main` by hand only when `eng land` cannot
be used, when it must be bypassed, each for a reason it can state, such as a broken queue or a fix
that cannot wait for it, or with the owner's approval. A fix that cannot wait for the gate lands
with `eng land --front --no-gate --why TEXT` instead, as above. A record or a document lands
by the queue like anything else. A hand push follows [push only onto the `main` you tested](#concurrent-git-integration)
and is reported to the owner with its reason. No check enforces this.

**Main moving under a passed batch.** The queue takes a hand push as it comes. If `main` moved only by Markdown documents the batch
does not touch, or by paths in `queue.carryOver`, each with why no queue lane reads it, the worker
checks that the batch's patch is unchanged on the new `main`, runs `eng docs check` when a document
moved, then the project's `queue.carryOverChecks`, and pushes. A carried-over result is no gate of the pushed commit: `eng release` will not
take it. Any other move gates again, with no blame. List in `queue.carryOver` only
records no queue lane reads, and check first: studywright's gate compiles sources under
`measurements/audits`, and BreezEPUB's script tests read parts of `evidence/`.

**A docs-only batch runs the docs lanes.** A project may declare on `main` which paths are
documentation, each with why no code lane reads it, and the lanes a batch that changes only those
paths runs instead of every queue lane:

```json
"docsOnly": {"paths": {"doc/guide/": "read by no code lane; eng docs check reads it",
                       "README.md": "read by no code lane; eng docs check reads it"},
             "lanes": ["docs"]}
```

When every path the batch changes, counting a moved file's old path, is a listed file or under a
listed directory ending in `/`, the worker runs only those lanes, in order, after the
spelling lane, the project's or the toolkit's, where they do not name it, since documents are where
words change, and lands the batch on their pass (owner's decision, 2026-11-02,
[#112](https://github.com/vocaro/vocaro-engineering/issues/112)). Several docs-only entries land
together as one batch. A batch with any other path, a mixed batch, and every batch of a project
that declares nothing run every lane, as does a batch that changes nothing. Only `main`'s
declaration counts, as with `hookCoveredBy`, so a branch cannot widen it. A project with
`hookCoveredBy` has said its hook is part of code lanes that read none of these paths, so a
docs-only batch skips the hook too; a project without it runs its hook as on any batch. A docs-only landing, like a carried-over
result, is no gate of the pushed commit: `gate` may not be a docs lane, so no gate receipt names
that commit and `eng release` will not take it; a release commit gets its gate from the release
procedure. The worker's log and `eng queue status` say an entry landed on the docs lanes only. List
only paths no code lane reads, and check first, as for `carryOver`: a path a docs check reads is
listed only when that check is a docs lane.

**Checks before a carry-over.** A project whose rule asks for more than the docs lane names the
commands in `queue.carryOverChecks`, in order, on the batch rebased onto the new `main`:

```json
"carryOverChecks": [
  {"command": ["python3", "tools/check_records.py", "--base", "{base}"], "paths": ["records/"]},
  {"command": ["python3", "scripts/eng", "docs", "check"]}
]
```

`command` is an argv list. `paths`, optional, limits a check to moves of those paths, each a file or
a directory ending in `/`, for a check too costly to run on every carry-over. Each runs through
eng's supervision, with `LANE_LIMIT` as its limit and a log of its own under `eng-queue/logs`, so
heavy work inside one goes through `eng admit` or `eng run` as anywhere else. A check that fails,
times out, cannot start, or finds no admission ends the carry-over: the batch gates again, with no
blame and no hold. A check is judged only by its exit status, so it must read what main's move
changed. Git diffs against `origin/main` read nothing of it, since by then the batch sits on that
`main`: `{base}`, anywhere in a word, is the commit the result passed on, and such a check takes it
as its base. Prove each check fails on a bad move before relying on it.

**State and recovery.** Each repository's state is under `eng-queue` in its common Git directory,
so every worktree sees one queue. A worker starts by itself with `eng land`, apart from the session,
and exits when nothing waits; `eng queue wait` and `eng queue resume` start one after a reboot.
Whatever a dead worker left testing was never pushed, so the next worker tests it again. A batch
whose lanes passed is recorded `passed`, with the commit that passed and its base, before its push;
a hold the push meets, a refusal or a remote out of reach, keeps it so, and the hold's reason says so.
The next worker, after `eng queue resume`, pushes that commit without gating it again, while the
batch is still the one that passed: an entry removed or added again since gates what is left. Main
having moved meanwhile is the carry-over's decision, as for any push. A lane may
take `LANE_LIMIT` (6 h, three times eng's default limit: the default for the lane and two more for
its wait for admission). The worker's checkout is locked, so that no sweep removes it, and keeps
ignored inputs a provisioning lane made between rounds. A project with `"integration": "pull-request"`
on `origin/main`, or with no gate lane on `origin/main` or in the commit added, is refused, and the
command says what to add. Neither `eng land` nor the worker reads any checkout's working tree for
this, so a main checkout that lags behind `origin/main` changes nothing: the worker reads its settings
from `origin/main` in its own checkout, and `eng run` there reads each lane from the batch.

## Working through a backlog

Asked to close as many issues as possible, triage before fixing.

- Read-only agents, one for each area, put every open issue in one class:
  - done on `main` already, with the commit and a file and line or test that shows it;
  - fixable and verifiable on this host;
  - needing a hosted run;
  - needing the owner's decision, with a recommended answer;
  - blocked.

  Each also names issues one change would close together, and issues that touch the same file.
  An agent's "already done" is a claim: check the commit and the code before closing on it.
- Close what `main` already does with a comment giving that evidence. File what is left of such
  an issue as an issue of its own first.
- Put decisions to the owner in batches, each option a recommendation with its reason and what
  closing then takes. A decision that changes nothing closes its issue with a comment.
- Give each fix agent the issues one change closes. Group them by the code they change, so that
  no two agents edit the same function, and hold an issue whose code an unlanded branch is
  rewriting until that branch lands.
- Budget disk as [scratch and result directories](shared-host.md#scratch-and-result-directories)
  says. Give each agent a scratch folder of its own, and have it delete its build folder once its
  commit is pushed.
- Land the finished branches through the [merge queue](#the-merge-queue), which gates them in batches.

On 2026-10-23 this closed 67 of a library's 205 open issues in one day: 20 by evidence comments
and the owner's decisions, and 47 by changes landed in four batches.

## Documentation and evidence ownership

Each kind of information has one home:

- What a system does, why, and its limits: the document for its subject. A repository has one
  document per subject, never one per issue or per release.
- How to run or repeat something: the subject's runbook, with probe scripts and their inputs in
  its probe section.
- What was run: a comment on the issue with the commit, the machine, the counts, the
  measurements and what was left unverified. A document links to that comment and does not
  repeat it.
- A release: its changelog section and GitHub release notes, which say what changed. They assume
  the gate passed and do not describe the run ([releasing](releasing.md#tagging-and-publishing)).
- Open work: an issue, never a document.

Documents describe the system as it is, in the present tense. The narrative of a change lives in
Git history; dated narrative belongs only in designated logs and measurement records. A build
number attached to a measurement is a fact about the measurement. Re-measure a figure before
documenting it, and give the build, corpus and configuration it was measured on. Put the current
result and the outstanding gaps first; label older pending text as historical and link failed or
superseded attempts to the final result, keeping their original revisions and outcomes.

**Measurements are records.** A measurement directory holds each experiment's record and the
small summaries it quotes. Raw captures (logs, archives, renders, converted outputs) stay out of
the tree, or are regenerated from the commands the record gives. A gate can cap what a change
adds there, counting committed, staged, unstaged and untracked additions against the base
branch. Tooling that a gate runs lives outside the records directory.

**Records cite commits and paths in prose, and stay frozen.** A record names the commit, paths
and identities it measured, as measured. The only later edit is a one-line pointer at the top
when a file it names has moved or been removed, or when a path or procedure it gives for
reproducing it has moved or changed; the pointer names where the current one is. A JSON record,
which cannot hold the line, gets the pointer at the top of its directory's record.md or README
when the directory has one. No manifest binds records to paths, no gate fails when a cited path
moves, and historical paths and hashes are never rewritten. Documents that need the current
truth state it themselves and link to the record for how it was established. A run's evaluation
receipt is a local consistency check binding one run to its own outputs, not a signed
attestation; a historical receipt that lacks required evidence is recaptured, not annotated.

**Decision records** state Context, Decision, Consequences and Evidence, take the next number,
and are listed in an index. A decision that supersedes another says so in both; neither is
deleted. Decision records are dated history, so checks that keep documents current exempt them.

**A ruled-out register** lists each rejected approach with its ruling and evidence, including
the build and corpus. An entry is removed only when new evidence overturns it, with the
overturning measurement recorded in the same commit.

**Deferring work keeps its coverage.** Deferring, parking or deprioritizing a class of work
means not starting it now. It never removes corpus cases, contracts, gates, tests or fixtures,
and a deferred area's cases still have to pass. Record the deferral on the issues it concerns;
removing coverage is a decision of its own, asked for and recorded as one.

**Issues.** The issue body is the goal as filed; later updates are comments, not body rewrites,
so the thread stays a record. Give each issue one kind of work. When research must precede
implementation, file two issues linked as blocked-by rather than one, so status and closure stay
unambiguous. File a defect with where it was found, the commit and exact command, what was
observed and expected, and the evidence; file only what was measured, and call a suspicion one.

**Agent entry points stay small.** Keep `AGENTS.md` under 32 KiB: Codex's default
`project_doc_max_bytes` silently truncates past that, and Claude Code warns above 40,000
characters. State a rule there and put its runbook, mechanism and evidence in the owning
document.

## Documentation checks

`python3 scripts/eng docs check [PATHS]` runs the shared check in
`src/python/vocaro_engineering/docs.py`. With no paths it reads the root Markdown files and
those under `doc`, `guides`, `knowledge`, `agents` and `policies`. It checks that:

- every local Markdown link resolves inside the checkout;
- the link's spelling matches the tracked name exactly, compared against Git's names because a
  case-insensitive APFS volume resolves a wrong-case path, and for untracked files component by
  component;
- a link's `#fragment` names an anchor of its Markdown target, or of the same document for a
  link that is only a fragment, so renaming or removing a section fails every link to it
  (`missing anchor`);
- a `PATH:LINE` link, such as one to `../Shaders/Grid.hlsl:180`, names a file that PATH
  resolves to as any link does and that has at least LINE lines (`no line 180 in a 150-line
  file`, `line link to a directory`). A target that is a file's whole name is that file. A
  range such as `Grid.hlsl:180-202` is no line link and fails as a missing link;
- every `AGENTS.md` is under 32 KiB (it fails at 32,768 bytes).

Anchors are GitHub's: each heading outside fenced code, ATX or setext, slugged by lowercasing
its rendered text, dropping everything but letters, marks, digits, `_`, `-` and spaces and
turning each space into a hyphen, with `-1`, `-2` added to a repeated heading; and every HTML
`id` or `name` attribute, such as `<a id="stacks"></a>`. A fragment on a target that is not
Markdown, such as a source line (`#L10`) or a link with a query, is not checked. It reads links
outside code blocks fenced with three backticks and skips URLs, which include a `PATH:LINE`
target with no `/` before its colon: `Grid.hlsl:180` is the scheme `grid.hlsl` to a browser
too, so write `./Grid.hlsl:180`. It does not mask inline code or `~~~` fences when finding
links, and it has no exemption list. Project checks add what a repository needs:

- **Snippets from compiled examples.** A document's code sample lives between
  `<!-- snippet:NAME -->` and `<!-- /snippet -->` and is regenerated from a
  `// snippet:start NAME` … `// snippet:end NAME` region of source that compiles. The check
  fails on a stale snippet, `--write` regenerates, and a minimum snippet count guards against a
  check that silently matched nothing.
- **Documented builds.** Compile every command a runbook prints, exactly as written, with only
  the output moved into scratch. Frozen records are exempt: they print the build they measured.
- **Generated counts.** Test and case counts live between `<!-- counts:NAME -->` markers and are
  computed from the suites, never from the text: a static Swift `@Test` count is checked against
  `swift test list` after the candidate's own debug build, because `--skip-build` in a warm
  checkout lists a previous candidate's products. A conflict confined to a region is resolved by
  regenerating; one that also touches prose is reported. A region present twice fails: a merge
  kept both sides. Counts still typed in prose are held to the contract they describe, and a
  phrase in a form the check cannot read fails rather than passing unread.
- **Fixture notices.** Notices name every fixture; a duplicated section or list (what a merge
  leaves), a list naming a missing fixture, a list behind its directory, a fixture of no listed
  family and a cited path the tree does not hold all fail.
- **Closed-issue citations.** A document may not cite a closed issue as the live tracker for a
  gap. Each allowed citation carries its reason, and an allowance no document cites fails. A
  checked-in issue-state snapshot keeps the check offline, so it never passes because the
  network was down; it prints the snapshot's age and fails the first citation of an issue newer
  than the snapshot. Refresh the snapshot in a commit of its own after a push closes a cited
  issue. A refresh learns every closure, not only its own, so run the check on the refreshed
  snapshot and push only a pass.
- **Exemptions.** Documents frozen by committed receipts and append-only logs whose dated
  entries name moved files get explicit, documented exemptions rather than rewrites.

## Spelling check

`python3 scripts/eng spelling [PATHS]` finds British spellings anywhere in a project's text,
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
`src/python/vocaro_engineering/spelling.py`.

It already exempts other projects' British-spelled API names, such as Apple's `isCanceled` and
Unity's `MonoBehaviour`, URLs, a list item's first `<em>` (a cited title) and a line marked
`spelling: keep` with a reason, such as a decoder reading a name stored before a rename. A
project lists the rest in engineering.json's `spelling`, each mapped to its reason: `skip` for
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

## Portable negative controls

An isolated checkout may itself live under a temporary directory. A test of a destination
outside permitted temporary roots must choose an actually excluded parent, rather than assume
the repository directory is excluded. Assert rejection before any write; a read-only existing
parent supplies a safe negative control. Preserve the positive temporary-directory and symlink
controls. The [StudyWright integration record](https://github.com/vocaro/studywright/issues/488)
captures the regression found while moving development methods into this toolkit.

Dependency-graph contracts also need explicit updates when a module moves. Keep the exact
shipping dependency closure and test-only exclusions enforced after accepting a new provider;
removing the negative controls would lose the boundary the extraction is meant to preserve.
