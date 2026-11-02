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
or your code.

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
python3 scripts/eng land                        # HEAD; returns at once, and the commit lands by itself
python3 scripts/eng land BRANCH --wait          # follow it: 0 landed, 1 came back, 75 held or still waiting
python3 scripts/eng land --repo EPUBLib BRANCH  # from any project: land a fix in another repository
python3 scripts/eng queue status
```

**A session does not run the full gate.** It runs the smallest relevant checks, commits and adds
the commit. Adding is the decision to land: the owner approves that once, and no later step asks.
What is added is the range the ref has and `origin/main` lacks, held by a ref of its own, so the
session may end and its branch may move or go. A merge in the range is refused. Adding a branch
again replaces what it added before.

**A fix in another repository lands there the same way.** Commit it in a worktree of that
repository, then `eng land --repo NAME BRANCH --wait`, from wherever the session is. It joins that
repository's queue beside its own sessions' commits and so stops none of their work. A commit must
be named, because HEAD there is another session's. `--wait` prints `landed: COMMIT` in full. Move
the dependent project's pin to that commit in a commit of its own, and land that in the dependent's
queue.

**A round.** The worker fetches, takes everything waiting, oldest first, cherry-picks each entry
onto `origin/main` in its own checkout, `../NAME-queue`, and runs the project's queue lanes through
`eng run`: its `spelling` and `provision` lanes when it has them, and then its `gate` lane, or
`queue.lanes` in engineering.json for a project that needs others, read from `main`, or from the
batch until `main` has any, so that the commit naming them can land. The
lanes therefore go where every lane goes, and one gate serves the whole batch. On a pass it pushes
exactly the commits it tested, as ordinary commits with their own messages and closing lines, so
Closeout finds each branch landed by equal patches. The gate receipt is of the pushed commit, which
`eng release` accepts, unless the result was carried over.

**Lines added at the top of a changelog section need no author.** Branches that each add a line at
the top of a section of `CHANGELOG.md`, in any directory and any case, conflict in turn, since each
line sits where the one before it just went. The worker settles such a conflict by keeping both, the
new lines first, and gates the result as usual. It does so only when every conflict in the commit is
in a changelog and each is lines both sides only added directly below a Markdown `#` heading, as
whole lines, with no heading and no blank line among them, and with none of the commit's lines already in the file, since
keeping both would double it. Anything else comes back, a change to an existing line or a conflict in
another file above all. The commit keeps its message and author. Its patch is then not the branch's,
so `landed.sh` proves no such branch landed, and it stays until its author removes it.

**Which toolkit a gate runs.** Each round, a project's worker moves `../NAME-queue-toolkit`, a locked
worktree of the toolkit with no session's edits, to the toolkit's `origin/main`, and runs the lanes, the
pre-push hook and the carry-over checks with `VOCARO_ENGINEERING_ROOT` naming it (owner's decision,
2026-11-01). A landed toolkit commit therefore reaches the next round of every project's gate, and a
shared toolkit checkout that lags or holds a session's changes reaches none of them. Each worker has
its own copy, so moving it changes no other project's running gate. The toolkit's own queue tests its
batch instead, and a `VOCARO_ENGINEERING_ROOT` set where the worker started stands. A copy that cannot
be fetched or moved holds the queue and blames no commit. Anything else still reads the shared checkout:
a session's own `eng`, and a project file that hard-codes its path. So a project commit that needs a new
toolkit option lands after the toolkit's commit, and runs by hand only once that checkout is pulled.

**What comes back to the author.** `eng queue status` and `eng queue wait` name each entry's state,
reason and log. An entry that conflicts with what is ahead of it comes back at once and costs the
others nothing. When a batch fails, the oldest half runs next, and so on until one entry fails
alone; every entry that passes along the way lands. An entry that comes back keeps its ref until
`eng queue remove ID`. Fix it, rebase and land it again.

**A run with no verdict blames nobody.** Exit 3 (unmeasured), 75 (no admission) and 78 (a
misconfigured host) say nothing about the commits. The worker tries once more and then holds the
queue, with every entry waiting, as it also does for a push refused for any reason but a race. A
hold stays until the owner, or a session the owner told to, has dealt with its cause and run
`eng queue resume`. Nothing fails over to another route.

**A pre-push hook runs ahead of the push.** `git push` connects before it runs the hook, so a hook's
long wait held an SSH connection open until GitHub dropped it. After a gate passes, the worker runs
the project's hook itself with `git hook run`, as Git would call it for pushing to `main`, with its
log in `eng-queue/logs`, and then pushes with `--no-verify`. A dropped connection or a race that
leaves the commit unchanged pushes again without running the hook again; a carried-over commit is a
new commit and runs it again. A hook's exit 3, 75 or 78 is no verdict, as a lane's is: it is tried
once more and then holds. Git passes the hook's status through, so a hook that waits for admission
should exit with the status it got rather than 1. Any other refusal holds at once, since the gate
passed and the hook disagrees, which is for the owner to read; it blames no commit.

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

**Pushing by hand is the exception.** A session pushes to `main` by hand only when `eng land` cannot
be used, when it must be bypassed, each for a reason it can state, such as a broken queue or a fix
that cannot wait for it, or with the owner's approval. A record or a document lands by the queue
like anything else. A hand push follows [push only onto the `main` you tested](#concurrent-git-integration)
and is reported to the owner with its reason. No check enforces this.

**Main moving under a passed batch.** The queue takes a hand push as it comes. If `main` moved only by Markdown documents the batch
does not touch, or by paths in `queue.carryOver`, each with why no queue lane reads it, the worker
checks that the batch's patch is unchanged on the new `main`, runs `eng docs check` when a document
moved, then the project's `queue.carryOverChecks`, and pushes. A carried-over result is no gate of the pushed commit: `eng release` will not
take it. Any other move gates again, with no blame. List in `queue.carryOver` only
records no queue lane reads, and check first: studywright's gate compiles sources under
`measurements/audits`, and BreezEPUB's script tests read parts of `evidence/`.

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
Whatever a dead worker left testing was never pushed, so the next worker tests it again. A lane may
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

It already exempts other projects' British-spelled API names, such as Apple's `isCancelled` and
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
told from a project's own name, so mark it: `case .cancelled:  // spelling: keep, URLError.Code`.
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
