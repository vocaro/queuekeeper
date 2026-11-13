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
| 75 | Busy or held; nothing ran. From `qk queue status --idle`, the queue is not idle |
| 78 | A host is misconfigured |
| 127 | A lane's command could not start |

## Output

- **Fixed status lines** end every run with the verdict, the test counts, up to twenty failing
  tests with file and line, and the path of the full log.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `queue status`, `queue doctor`, `queue placement`, `queue events`,
  `admissions now` and `doctor`.
- **Nothing prompts.** Commands are meant to run in the background and be followed to their result.
- **`qk queue status --idle` answers by exit status** whether the queue is idle, for a script that
  must not end a running gate, such as one that removes the queue's login agent
  ([#21](https://github.com/vocaro/queuekeeper/issues/21)). It prints the lines `qk queue status`
  prints and exits 0 only when no worker runs and no entry is queued, testing or passed, and 75
  otherwise. A live worker between rounds with nothing waiting is not idle, and neither is a held
  queue with entries waiting. A worker that died leaves no worker: its lock is released with it.
  With `--json`, the `idle` field says the same.

## Verifying

A verification run goes through admission, so that it takes a slot and `qk admissions now` shows
it, in one of three ways:

- `python3 scripts/qk unittest -s tests NAME...`, which admits itself as `qk admit` would; NAME is
  a module, class or test, such as `test_merge_queue.Landing`;
- a lane, `python3 scripts/qk run LANE -- FILTER`;
- any other command, `python3 scripts/qk admit -- COMMAND`.

It never goes straight through `python3 -m unittest`. That takes no slot, so the other sessions'
admitted work waits behind load nobody can see, and it misses what `qk unittest` does: its
summary, the log it keeps of a failed run, its processes side by side, and the suite's own setup.
On 2026-11-13 a coordinator's `qk admit -- python3 -m unittest tests.test_merge_queue` failed at
once on the second Mac with `ModuleNotFoundError: No module named 'host_isolation'`: a module named
from the top of the checkout cannot import the setup beside it in `tests/`, which
`qk unittest -s tests` puts on the path.

Nothing is small enough to run directly. One test or one class goes through `qk unittest` as the
whole suite does, and its cost is the wait for a slot, which every lane takes however short. Only
a test of a runner itself runs with `--unadmitted`, written after the runner's name
([Heavy-work admission](../guides/shared-host.md#heavy-work-admission)). Run outside any admission
and outside `qk unittest`, this repository's tests say so once on standard error and run on
(vocaro/queuekeeper#36).

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
- **An interrupted wait withdraws nothing.** `qk land --wait` only follows the entry; the queue's
  worker lands it. When the waiting command ends early, because the Mac restarted, the session's
  turn was interrupted or its terminal closed, the entry carries on and lands if its gate passes.
  Pick up its result without that command: `qk queue status` lists the entry, waiting or recently
  settled, with the commit it landed as; `git fetch` brings that commit to `origin/main`; and
  `python3 scripts/qk queue wait ID` follows the entry to its result with the same lines and exit
  statuses as `qk land --wait`, or, for an entry already settled, says at once how it ended, with
  0 for landed and 1 for came back. Queue the commits again only after confirming that the entry
  came back or was never queued (vocaro/queuekeeper#120).
- **`qk land BRANCH --wait` again, while the branch's entry waits, adds nothing.** When an entry
  still queued, testing or passed holds the same tip, it prints `ID already holds TIP (STATE)` and
  follows that entry as `qk queue wait ID` would, as a session that ran it again after a crash on
  2026-11-13 was told `… already holds … (testing)`. `--front` and `--no-gate` given again change
  only an entry still queued. A branch whose tip has changed since is another matter: its new
  commits replace the earlier entry, and stop that entry's gate if it is testing. A tip
  `origin/main` already holds is not queued (`origin/main already holds TIP: nothing to queue`,
  exit 0). Main holds a landed branch's commits under other hashes, so a branch that has landed is
  queued again, and the next round records that entry as landed, since `origin/main` already
  holds its changes.
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

## Documentation changes

An entry that changes only documentation skips the code gate. It lands on the spelling check and
the documentation lanes, in seconds, beside a code gate that is running. One other path in the
entry makes the whole entry ride the code gate:

- **Documentation is Markdown in the usual places**: the root `README.md`, `CONTRIBUTING.md` and
  `CHANGELOG.md`, and `.md` or `.markdown` files under `doc/`, `docs/`, `guides/` and `knowledge/`.
  A project can name its own paths with `queue.docsOnly` on `main`.
- **Everything else is code to the queue**: source, tests, configuration, agent instructions such
  as `AGENTS.md`, a stylesheet, a script, and an image or diagram a document shows. A README
  change committed with the screenshot it adds rides the code gate.
- **Queue documentation as its own entry.** When a document change and a code change do not depend
  on each other, commit the document change on a branch of its own, started from `origin/main`,
  and land that branch separately. It lands without waiting for the code, and the code entry is no
  larger for it. Keep them in one entry when the document describes behavior the code change
  brings, so that neither lands without the other.
- **A documentation landing is not a gate.** No gate receipt names the pushed commit, and
  `qk queue status` records the entry as a documentation landing with the lanes it ran.

[While a gate runs](merge_queue.md#while-a-gate-runs) says how such an entry lands beside a gate,
and [Maintenance](../guides/maintenance.md#the-merge-queue) has the rule in full, under *A docs-only
batch runs the docs lanes*.

See also [The merge queue](merge_queue.md).
