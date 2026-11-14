# The merge queue

How a commit gets from an agent's branch to `main`, and what happens when it does not.

## Landing a commit

An agent commits its work on any branch and runs `qk land`. That queues the commits the
branch has and `origin/main` lacks, held by a Git ref of their own, so the agent's session can end
and its branch can move or be deleted. One worker per repository does the rest.

```sh
qk land --wait          # queue HEAD and follow it to its result
qk land BRANCH          # queue a branch and return at once
qk queue status         # what is waiting, what is testing, what came back and why
qk queue status --idle  # the same, exiting 0 only when no worker runs and nothing waits, else 75
qk queue remove ID      # take an entry out
```

Each round, the worker:

1. fetches `origin/main` and takes everything waiting, oldest first, as one batch;
2. cherry-picks the batch onto `origin/main` in a checkout of its own;
3. runs the project's gate lanes once for the whole batch, on whichever Mac has room;
4. pushes exactly the commits it tested, each with its own message and author.

Queue state is kept in the repository's Git directory, so every worktree sees the same queue. A
worker that dies loses nothing: whatever it was testing was never pushed, and the next worker tests
it again. A batch that passed is recorded before its push, so an outage at the remote does not
discard the result.

## Checked before it is queued

`qk land` refuses an entry in seconds, with nothing queued, when a round would only send it
back later:

- **Conflicts.** It makes the integration a round would make, onto `origin/main` and every entry
  ahead, in a scratch worktree. A refusal names the files and lines in conflict and the entries
  ahead that change them. With `--wait`, it waits for those entries to land and tries again. The
  check took 0.6 to 6 seconds in measurements on trees of 200 to 10,000 files.
- **Commit messages that close an issue by mistake.** "This does not fix #43" closes issue 43 on
  GitHub. Such a message is refused, with the line and what to write instead.
- **Tests that could not run.** A test module that does not parse would make the whole gate run
  nothing, so it is refused up front.
- **Lint.** The spelling lane runs on the entry before the gate does.

## When a batch fails

The worker tries the cheap explanations before the expensive one:

1. **Flake.** It reruns each failed test alone, up to three times. If every rerun passes, and no
   commit in the batch changes the test's module or a file that module imports, the batch lands.
   The flake is recorded in the queue's status, in a `Flaky-test:` trailer on the landed commit, and
   as a comment or new issue on the tracker. A test the batch adds, which `origin/main` does not
   have, is no flake: the entry that adds it is sent back to fix it, and nothing is filed.
2. **One obvious culprit.** If the failure is real and every failed test points at the same entry,
   by a path its output names or a file it imports, that entry is sent back and the rest gate again
   at once.
3. **Bisect.** Otherwise the batch is halved until one entry fails alone. Both halves gate at the
   same time, on different Macs, and every entry that passes along the way lands.

Steps 1 and 2 read test names from the output of `qk unittest`, the bundled Python test
runner. A failure in any other kind of lane goes straight to the bisect.

An entry sent back is told the load during the failed lane, the failed tests, and whether its
commits change their files. For a lane other than `qk unittest`, such as a Swift gate, the tests and
the files they failed in come from the summary that ends the lane's `qk run`.

## While a gate runs

- **Stacked gates.** Entries that arrive during a gate are tested on top of the batch's tip, on
  another Mac. If the batch lands, the stacked gate becomes the next round's gate and is already
  under way. If it does not, the stacked gate is thrown away. Only one runs at a time.
- **Documentation does not wait for code.** An entry that changes only Markdown documents lands
  beside a running code gate, after the documentation lanes pass. If `main` moves only by documents
  while a code batch gates, the batch's result carries over without a second gate.
- **Removing an entry** stops its gate within seconds and frees the Mac.

## No verdict is not a failure

A lane ends as passed, failed or unmeasured. A timeout, a lost connection, a Mac too busy to admit
the lane, a misconfigured host and a screen that locked during a UI test are all unmeasured: they
say nothing about the commits, so nobody's entry is sent back. The worker waits for the Mac to
settle (CPU busy share under 75%, normal memory pressure) and runs the lane again.

After three runs in a row with no verdict, the queue holds with every entry still waiting. A hold
states its reason and who can clear it: an agent for most causes, the owner only for causes an agent
cannot fix, such as a refused SSH key. Whoever fixes the cause runs `qk queue resume`.

A remote that cannot be reached is retried with backoff for about an hour before the queue holds.
During a GitHub incident, the worker reads GitHub's status page and keeps trying while Git
operations are degraded.

## Emergencies

- `qk land --front` tests an entry next, ahead of everything waiting.
- `qk land --no-gate --why "..."` lands with only the lint lane and the pre-push hook. The
  reason is required and is recorded in a `Landed-without-gate:` trailer. Status lists the commit as
  ungated until a later gate covers it.
- With both, the entry lands next. It goes ahead of a round whose gate still waits for admission,
  which then gates again on top of it, and waits only for a gate that has started, which status
  says.

## A queue that looks stuck

`qk queue doctor` reads the queue's own records and says whether it is testing or stuck,
and what would unstick it. It names a gate that has waited 15 minutes for admission on a
Mac with spare CPU, and each holder there with its command, PID, hold time, CPU use and
worktree. `qk queue placement` shows whether each build Mac could start a
gate now, without running one.

`qk queue events` prints the queue's journal, one line for each change of its state. See
[Visibility](visibility.md).

See also [Lanes and admission](lanes_and_admission.md), [The build farm](build_farm.md) and
[The agent interface](agent_interface.md).
