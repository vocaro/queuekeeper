# The agent interface

Queuekeeper's users are mostly AI agents, so its output is written to be acted on without a person
reading it.

## Exit codes

Exit codes separate the cases an agent must treat differently:

| Code | Meaning |
|---|---|
| 0 | Passed or landed; for `queue result` and `queue logs`, the read succeeded |
| 1 | Failed or came back; for `queue logs`, the log is unavailable or the cursor is stale |
| 2 | Usage or configuration error |
| 3 | Unmeasured |
| 4 | Failed before any test started |
| 70 | Queuekeeper itself could not start: its plugins or one of the project's extensions did not load, so nothing ran |
| 75 | Busy, held, or the wait reached its limit. From `qk queue status --idle`, the queue is not idle |
| 78 | A host is misconfigured |
| 127 | A lane's command could not start |

## Output

- **Fixed status lines** end every run with the verdict, the test counts, up to twenty failing
  tests with file and line, and the path of the full log. A run that failed or ended unmeasured
  with no test counted or named also quotes the last lines of its command's output.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `land`, `queue add`, `queue wait`, `queue result`, `queue status`,
  `queue logs`, `queue doctor`, `queue placement`, `queue events`, `admissions now` and `doctor`.
- **Nothing prompts.** Commands are meant to run in the background and be followed to their result.
- **`qk queue status --idle` answers by exit status** whether the queue is idle, for a script that
  must not end a running gate, such as one that removes the queue's login agent
  ([#21](https://github.com/vocaro/queuekeeper/issues/21)). It prints the lines `qk queue status`
  prints and exits 0 only when no worker runs and no entry is queued, testing or passed, and 75
  otherwise. A live worker between rounds with nothing waiting is not idle, and neither is a held
  queue with entries waiting. A worker that died leaves no worker: its lock is released with it.
  With `--json`, the `idle` field says the same. An entry can arrive after an idle reading, so
  maintenance that needs the queue to stay empty runs `qk queue close --why TEXT` first and
  `qk queue reopen` after; status says `closed to new entries by WHO since WHEN: WHY`, and its
  `closed` field holds `why`, `by` and `at`, or null while the queue is open.

## Structured results and recovery

Use `qk land --wait --json` for a single JSON result on standard output. Progress and
diagnostics go to standard error. `qk queue add --json` returns the submitted entry immediately;
`qk queue wait ID --json` follows an existing entry. These commands retain their text-mode exit
statuses. A wait for several IDs returns an `entries` array.

`qk queue result ID --json` reads a specific entry, including old settled entries outside the
status view's recent window, without starting a worker. Its exit status is 0 when the lookup
succeeds, even for a failed or pending entry; inspect `state` and `terminal` for the outcome.
An unknown ID exits 2. Use `queue wait` when the exit status must reflect the landing verdict.

Entry results name the submitted `commit`, `landedAs`, failure details, test counts when known,
and the exact run's artifacts. An artifact's `location` says where its path can be read;
`producedOn` identifies a host that produced a returned file. Missing or pruned artifacts remain
listed with `available: false`. Diagnostic excerpts are bounded; `logTruncated` says when they
omit earlier bytes. A result is a summary, not a replacement for the complete log.

The `flakes` array preserves each recorded flaky test's name, commit, gate and rerun log paths,
Mac, batch outcome, and tracker link (`issue`) or `trackerError`. Its `reruns` object gives
`status: "passed"` and the recorded rerun `count`. These are successful isolated reruns, not a
failed entry verdict; `failure` remains null for a landing. Missing historical test counts and
result summaries remain null, and unrecorded artifact paths are not invented. An entry with no
recorded flakes returns an empty array.

Status and entry results carry `schemaVersion: 1`. Readers should tolerate added keys and
unrecognized states or reason codes. Existing status keys, including the `held` explanation,
remain available. The nested `recovery` object supplies:

- `reasonCode`: a category such as `queue-held`, `wait-limit` or `entry-failed`;
- `responsibleParty`: `owner`, `session`, `unknown`, or null when no action is needed;
- `retryable`: whether the same operation can be repeated without first repairing a cause;
- `action`, `message`, and `suggestedCommands`: the next step, with commands as argument lists.

A held queue suggests diagnosis, never an unconditional resume. An expired wait leaves the
entry queued and suggests following that entry again. Suggested commands name their project
explicitly, including after `land --repo`.

## Reading logs without losing earlier output

Terminal output may be shortened by an agent's tools. `tail` shows only the end of a file, which
can hide the first failure. Keep the entry ID and use `qk queue result ID --json` for its verdict,
failure summary and artifact references, including after an interrupted wait. A successful log
read is not evidence that the gate passed.

To inspect the full log in bounded pieces, start at its beginning:

```sh
qk queue logs ID --json --limit 16384
```

The response includes `content`, the selected `artifact`, `nextOffset`, `nextCursor` and
`hasMore`. Pass the returned cursor unchanged to continue:

```sh
qk queue logs ID --json --cursor CURSOR
```

Retain each chunk before advancing its cursor. When `hasMore` is false, the cursor can still
read output appended later; check `queue result` to know whether the entry has settled.
`waitingForBytes` means an unfinished UTF-8 character is waiting for the writer to finish it;
wait before asking again rather than spinning. A settled file's incomplete final character is
shown as a replacement character so reading can finish.

The cursor identifies the file and byte position and checks the last returned chunk. If the file
is replaced, truncated or that chunk changes, the command reports `log-cursor-stale`; restarting
from the beginning is explicit. `--offset N` can also seek to a byte position, but lacks these
cursor checks. Use the returned offsets so that a position never splits a UTF-8 character.

Reads default to 16,384 bytes and accept `--limit` from 4 to 1,048,576 bytes. With several logs,
the default preference is a live gate, an exact failed dispatch's run log, the entry's primary
gate log, then its latest recorded lane log. Earlier attempts remain listed as artifacts, while
the result's diagnostic summary uses the final attempt's evidence.
`--artifact KIND` or `--artifact PATH` selects a listed log; use its full path when several have
the same kind. A missing or unreturned remote log is reported as unavailable. Reads never fetch
from another Mac or substitute a newer run's log.

The log command exits 0 for a successful read, 1 for an unavailable log or stale cursor, and 2
for an invalid request. Its JSON errors include `reasonCode` and the available artifact references.
The default text mode prints content on standard output and continuation information on standard
error. These are Queuekeeper commands built on file offsets, rather than a Unix `tail` option.

## Verifying

A verification run goes through admission, so that it takes a slot and `qk admissions now` shows
it, in one of three ways:

- `python3 scripts/qk unittest -s tests NAME...`, which admits itself as `qk admit` would; NAME is
  a module, class or test, such as `integration.test_merge_queue.Landing`;
- a lane, `python3 scripts/qk run LANE -- FILTER`;
- any other command, `python3 scripts/qk admit -- COMMAND`.

It never goes straight through `python3 -m unittest`. That takes no slot, so the other sessions'
admitted work waits behind load nobody can see, and it misses what `qk unittest` does: its
summary, the log it keeps of a failed run, the progress it keeps while it runs
([Long runs](#long-runs)), its processes side by side, and the suite's own setup.
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

Nor does a session wait for a quiet Mac before it starts. The load average on these Macs counts
hundreds of simulator threads and stays in the hundreds under ordinary work: on 2026-11-12 a loop
that waited for a one-minute load under 10 would never have fired, while `qk run gate` passed on
the second Mac in 925 s. Admission waits for a slot and chooses the Mac, so a session never polls
`uptime`, `sysctl vm.loadavg` or `top`, and asks `qk admissions now` when it needs to know why its
work has not started. Exclusive work settles before its command starts and tells the command what
it read of the Mac (vocaro/queuekeeper#121)
([Ask admission, not the load average](../guides/shared-host.md#ask-admission-not-the-load-average),
vocaro/queuekeeper#134).

## Long runs

A long run's progress has to stay readable while it runs, so that an agent or the owner can say
whether it is moving and how far it has got. In the run vocaro/queuekeeper#22 reported on
2026-11-12, an agent ran `python3 -m unittest test_remote 2>&1 | tail -6` on a Mac at a load
average of about 700. It ran
for more than two hours, and the only sign that it was alive was new processes in `ps`: `tail`
prints nothing until its input ends, and unittest's dots name no test.

- **Start it with its output in a file**, `COMMAND > FILE 2>&1`, as a background command, and trim
  when reading the file, not when writing it. Never pipe a long run through `tail` or `head`.
- **`qk unittest` keeps its own progress whatever its output is.** Its first line, before any work
  starts, is `progress: FOLDER keeps this run's progress`. For a bare `qk unittest` or another
  runner admitted first, the folder is the one its admission keeps on this Mac. Either is under
  `.build/queuekeeper/runs/` in the directory the run started in. When standard output is a pipe,
  the line comes on standard error too, so that a run piped into `tail` still says where to look.
  `progress.log` there gets a line as each process ends, such as `212 of 528 processes, 1840
  tests, 0 failed, 0 unmeasured, 4 running: test_remote: OK (276 tests, 812.4s)`, with the
  process's whole output under it, and `progress.json` says the same with the processes running
  now. An admitted run's folder holds `output.log` too, everything the run printed as it printed
  it, which is all this Mac sees of a run the second Mac took.
- **`qk progress` answers "how far along is it?"** from another shell. With no argument it reads
  each run still running under `.build/queuekeeper/runs/`, in the current directory and the
  project's root, or the newest one when none runs, and prints its command, how long it has run,
  the processes and tests finished and failed, the processes running and for how long, and when
  it last moved. `qk progress FOLDER` reads the run the first line named; `--all` reads every one
  kept. The newest twenty ended runs are kept, and every run still running.

`qk run` keeps each lane's log in `.build/queuekeeper/LANE/run.log` as before, and
`qk land --wait` prints a line whenever the entry's situation changes; neither yet names a
progress folder.

## Landing

`qk land --wait` follows a commit to its result, so an agent starts it in the background and is
woken by its end:

- **A refusal before queuing is one line**, `COMMIT is not queued: REASON`, with exit status 1: a
  preflight lane's finding, a closing keyword GitHub would act on, a check `qk unittest` would refuse
  every gate for, or a merge that brings in another branch's work. Nothing was queued. A branch
  refreshed by merging `main` is not refused: the queue lands its own commits in a line.
- **A queue closed for maintenance refuses the same way**, with who closed it, when and why:
  `COMMIT is not queued: the queue is closed to new entries by WHO since WHEN: WHY`
  ([Closing a queue for maintenance](merge_queue.md#closing-a-queue-for-maintenance)). Entries
  already queued finish. Land again once `qk queue status` no longer says closed; whoever closed
  the queue reopens it, not the session it refused. With `--json`, the result's `state` is
  `refused` and its `recovery` has `reasonCode: "queue-closed"`, `action: "wait-for-reopen"` and
  the closure as `closed`, with `why`, `by` and `at`.
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

A lane in which Queuekeeper itself could not start holds the queue at once, for a session, with every
entry waiting and none sent back: `qk run` exited 70 and left no receipt of the lane, so no commit
was tested. The reason quotes the error, names the Mac, and says to bring Queuekeeper and the folders
the project's extensions load from to commits that work together
([Maintenance](../guides/maintenance.md#the-merge-queue), *A lane Queuekeeper could not start for
blames nobody*). An entry whose own commits stop Queuekeeper from starting is not held for: the
worker finds that Queuekeeper starts without the batch, and sends the entry back as for a failed lane.

## Documentation changes

An entry that changes only documentation skips the code gate. It lands on the project's preflight lanes
and the documentation lanes, in seconds, beside a code gate that is running. One other path in the
entry makes the whole entry ride the code gate:

- **Documentation is Markdown in the usual places**: the root `README.md`, `CONTRIBUTING.md` and
  `CHANGELOG.md`, and `.md` or `.markdown` files under `doc/`, `docs/`, `guides/` and `knowledge/`.
  A project can name its own paths with `queue.docsOnly` on `main`.
- **Everything else is code to the queue**: source, tests, configuration, agent instructions such
  as `AGENTS.md`, a stylesheet, a script, and an image or diagram a document shows. A README
  change committed with the screenshot it adds rides the code gate, and runs the documentation
  lanes too, ahead of the gate, so that a document the documentation lanes would fail never lands
  with code.
- **Queue documentation as its own entry.** When a document change and a code change do not depend
  on each other, commit the document change on a branch of its own, started from `origin/main`,
  and land that branch separately. It lands without waiting for the code, and the code entry is no
  larger for it. Keep them in one entry when the document describes behavior the code change
  brings, so that neither lands without the other.
- **`qk land` says which gate an entry will ride** as it queues it, on standard output after the
  `queued ID` line, by `main`'s settings as a round reads them (vocaro/queuekeeper#118). An entry
  of documentation alone gets `queue: ID is documentation only: lands on the spelling and docs
  lanes, not the code gate`, naming its preflight lanes before the docs lanes. Any other entry gets `queue: ID rides the code gate: PATH is not
  documentation`, naming the first of its paths, in sorted order, that is not documentation, and
  adds `; the docs lane runs too, ahead of it, for its documents` when it changes documents as
  well. When that path should not have been in the entry, move it to an entry of its own, as
  below. An entry landing with `--no-gate` gets no such line: it says it lands alone on the
  preflight lanes.
- **A documentation landing is not a gate.** No gate receipt names the pushed commit, and
  `qk queue status` records the entry as a documentation landing with the lanes it ran.

[While a gate runs](merge_queue.md#while-a-gate-runs) says how such an entry lands beside a gate,
and [Maintenance](../guides/maintenance.md#the-merge-queue) has the rule in full, under *A docs-only
batch runs the docs lanes*.

See also [The merge queue](merge_queue.md).
