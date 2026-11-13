# The agent interface

Queuekeeper's users are mostly AI agents, so its output is written to be acted on without a person
reading it.

## Exit codes

Exit codes separate the cases an agent must treat differently:

| Code | Meaning |
|---|---|
| 0 | Passed, or landed |
| 1 | Failed, or came back |
| 2 | Usage or configuration error |
| 3 | Unmeasured |
| 4 | Failed before any test started |
| 75 | Busy or held; nothing ran |
| 78 | A host is misconfigured |
| 127 | A lane's command could not start |

## Output

- **Fixed status lines** end every run with the verdict, the test counts, up to twenty failing
  tests with file and line, and the path of the full log.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `queue status`, `queue doctor`, `queue placement`, `queue events`,
  `admissions now` and `doctor`.
- **Nothing prompts.** Commands are meant to run in the background and be followed to their result.

## Landing

`qk land --wait` follows a commit to its result, so an agent starts it in the background and is
woken by its end:

- **A refusal before queuing is one line**, `COMMIT is not queued: REASON`, with exit status 1: a
  spelling finding, a closing keyword GitHub would act on, a check `qk unittest` would refuse
  every gate for, or a merge that brings in another branch's work. Nothing was queued. A branch
  refreshed by merging `main` is not refused: the queue lands its own commits in a line.
- **A send-back ends the wait within seconds**, naming the files in conflict and the entries
  ahead, or the failed tests and the log.
- **While it waits, it prints a line each time the entry's situation changes**, and so does
  `qk queue wait`. Each line begins with the entry's ID and stands alone: waiting behind a round,
  with that round's entries and how long it has run; in its own round, with the gate's lane and
  Mac, or what the gate waits for; its batch halved after a failure; a stacked gate; passed and
  waiting for the push. It never says the same situation twice. While nothing changes, a line
  beginning `heartbeat:` comes at most every 5 minutes with what `qk queue status` shows of that
  round: each gate's lane, Mac, running time, what it waits for and its progress. These lines
  come before the result, which they leave as it was: the result lines and the exit status.
- **`qk queue status` is the whole picture**: failed entries and the current round as well as the
  running gate. The queue's heartbeat cannot see a lane on another Mac advance, so for a gate
  there, read the lane's own log on that Mac before calling a landing stuck.
- **Each project's launcher lands its own repository**, and refuses a current directory in
  another. A commit for another repository lands in that repository's queue, with
  `qk land --repo NAME BRANCH --wait`.
- **A worker follows Queuekeeper's `origin/main`** at the start of each round, so one started
  from an older checkout does not hold the queue on a key the newer one knows.

## Holds

When the queue stops, the hold's reason begins with whom it is for. Most holds are for an agent,
which fixes the cause and runs `qk queue resume` itself. A hold is for the owner only when
an agent cannot fix the cause, such as a refused SSH key. With the
[notify feature](visibility.md#the-notify-feature) on, the owner is told of such a hold when it
happens.

See also [The merge queue](merge_queue.md).
