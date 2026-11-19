# Queuekeeper user guide

Everything you need to set up Queuekeeper, land changes through it, and follow what it is doing.
The [README](../README.md) explains what Queuekeeper is for. The [developer guide](developer_guide.md)
covers how it works inside and how to change it.

- [Set up](#set-up): requirements, install, configure a project, tell your agents, add Macs
- [Land changes](#land-changes): what the merge queue does with a commit
- [Lanes and admission](#lanes-and-admission): the commands Queuekeeper runs, and when a Mac has room
- [The build farm](#the-build-farm): running work on more than one Mac
- [For agents](#for-agents): exit codes, structured results, logs and verification
- [Watch the queue](#watch-the-queue): the terminal view, notifications and the dashboard
- [Troubleshooting](#troubleshooting): holds, a stuck queue, disk space

## Set up

### Requirements

- **macOS.** Queuekeeper reads macOS's load, memory-pressure and screen-lock state. It has not
  been ported to Linux or Windows.
- **Python 3.14 or newer**, and no other Python packages. Apple's built-in `python3` is too old,
  so install a current one, for example with Homebrew.
- **Git**, and a repository that:
  - has a remote named `origin` you can push to without a prompt, and
  - lands changes directly on `main`. Queuekeeper refuses a project that integrates through pull
    requests.
- **A command that tests the project** and exits nonzero when it fails. This becomes the gate.

The Apple plugin has [requirements of its own](../plugins/apple/src/queuekeeper_apple/README.md#requirements).

### Install

Clone the repository and run its setup script with the Python that will run Queuekeeper:

```sh
git clone https://github.com/vocaro/queuekeeper.git
cd queuekeeper
python3 tools/install-checkout.py
qk --help
```

The script:

- puts the `qk` command on your PATH, in `~/.local/bin` unless another writable folder on your
  PATH comes first, and tells you if that folder is not on your PATH yet;
- makes the `queuekeeper` package importable for projects whose own scripts use it;
- gives this Mac a CPU limit of 70% (`"busyLimit": 70` in its limits file), on the assumption
  that it is the Mac you work at. Macs you add later keep the default of 90%, so they take more
  of the load. A limits file that already sets `busyLimit` is left alone.

Keep the checkout: `qk` runs it. To update, run `git pull` in it.
`python3 tools/install-checkout.py --check` says whether everything is in place, and `--remove`
takes it out again.

### Configure a project

Queuekeeper reads `queuekeeper.json` at the root of each project. The smallest useful file names
one lane, `gate`, the command every change must pass:

```json
{
  "schemaVersion": 1,
  "name": "my-project",
  "extensions": [],
  "lanes": {
    "gate": {"command": ["scripts/test.sh"], "timeout": 1800}
  }
}
```

| Key | Meaning |
|---|---|
| `schemaVersion` | Always `1` |
| `name` | The project's name |
| `extensions` | The [plugins](../README.md#plugins) to load. `[]`, or leaving it out, loads the core alone |
| `command` | An argument list, not a shell string |
| `timeout` | Seconds. A lane that reaches it ends as *unmeasured*, not failed |

The queue always reads its settings from `main`, so a branch cannot loosen its own gate. The one
exception is the commit that adds `queuekeeper.json`, which is gated by its own file since `main`
has none yet ([A project's first landing](guides/merge-queue.md#a-projects-first-landing)).

Then check the setup:

```sh
qk doctor
```

It reports missing commands, lanes without a timeout, tests that no lane runs, and problems on
any Mac.

Two defaults to know about:

- **Preflight lanes.** List the project's cheap checks, such as a linter, in `queue.preflight`.
  The queue runs them before it accepts an entry and first in every round
  ([Preflight lanes](#preflight-lanes)). Give `"queue": {"preflight": []}` for none. Until you
  set it, the queue runs a built-in spelling check, which a later release removes.
- **Documentation-only changes** skip the gate and run the documentation lanes instead
  ([Documentation changes](#documentation-changes)). Set `"queue": {"unread": false}` to gate
  every change.

[Lanes and admission](#lanes-and-admission) describes the other lane settings.

### Tell your agents

Agents have to be told to queue their work instead of pushing it. Add a rule like this to the
instructions they read, such as `AGENTS.md` or `CLAUDE.md`, adapting the lane names:

```markdown
Never push to main. Commit on your own branch, then run `qk land --wait` as a
background command and follow it to its result. Exit 0 means landed, 1 means it came back
with a reason, and 75 means the queue is held or still waiting. If the wait is interrupted, the
entry still lands: follow it with `qk queue wait ID`, or run the same `qk land --wait` again,
which follows the entry already queued. Run tests and builds with `qk run LANE` or
`qk admit -- COMMAND`, never bare, so that they wait their turn on the shared Macs. Run unittest
suites with `qk unittest`, never `python3 -m unittest`. Send a long run's output to a file, never
through `tail` or `head`, and trim when reading it; `qk progress` says how far a run has got.
```

[For agents](#for-agents) has the exit codes and output they can rely on.

### Land a first change

Have your agent commit the configuration and agent instructions on its branch, run
`qk land --wait`, follow the entry to its result, and confirm the setup reached `origin/main`.

### Add another Mac

Once changes land on one Mac, you can add more:

1. On the new Mac, turn on Remote Login and add the first Mac's public key to
   `~/.ssh/authorized_keys`.
2. On the first Mac, add the new one by any SSH destination that reaches it without a prompt,
   a host name or an alias in `~/.ssh/config`. It is called by its own name unless you pass
   `--name`:

   ```sh
   qk farm add NAME.local
   ```

3. Install the tools it needs, then check every Mac:

   ```sh
   qk remote setup
   qk doctor
   ```

From then on each lane runs on whichever Mac has room ([The build farm](#the-build-farm)).

### Keep the queue running

A queue worker starts by itself when you run `qk land` and exits when nothing is waiting. To also
check for waiting work at login and every two minutes, set up the
[signed queue launcher](guides/maintenance.md#signed-queue-launcher). It also avoids a macOS
restriction that can cut a background worker off from the local network
([macOS Local Network privacy](#macos-local-network-privacy)).

## Land changes

### How a landing works

An agent commits on any branch and runs `qk land`. That queues the commits the branch has and
`origin/main` lacks. The queue holds them by a Git ref of its own, so the agent's session can end
and its branch can move or be deleted.

```sh
qk land --wait              # queue HEAD and follow it to its result
qk land BRANCH              # queue a branch and return at once
qk queue status             # what is waiting, testing, and what came back and why
qk queue wait ID            # follow an entry already queued
qk queue remove ID          # take an entry out
```

One worker per repository then repeats a round:

1. Fetch `origin/main` and take everything waiting, oldest first, as one batch.
2. Cherry-pick the batch onto `origin/main` in a checkout of its own.
3. Run the project's gate once for the whole batch, on whichever Mac has room.
4. Push exactly the commits it tested, each with its own message and author.

Queue state lives in the repository's Git directory, so every worktree sees the same queue. A
worker that dies loses nothing: what it was testing was never pushed, and the next worker tests
it again. A batch that passed is recorded before its push, so an outage at the remote does not
throw the result away.

### Checks before queuing

`qk land` refuses an entry within seconds, with nothing queued, when a round would only send it
back later:

- **Conflicts** with `origin/main` or any entry ahead. The refusal names the files, lines and
  entries in conflict. With `--wait`, it waits for those entries to land and tries again.
- **Commit messages that close an issue by mistake**, such as "This does not fix #43", which
  GitHub reads as closing issue 43.
- **Tests that cannot run**, such as a test module that does not parse.
- **Preflight findings** ([Preflight lanes](#preflight-lanes)).

A refusal is one line, `COMMIT is not queued: REASON`, with exit status 1. A branch refreshed by
merging `main` is not refused; the queue lands its own commits.

### Preflight lanes

Preflight lanes are a project's cheap checks: a spelling check, a linter, a formatter in check
mode. The project names them, in order, in `queue.preflight` on `main`:

```json
"lanes": {
  "spelling": {"command": ["python3", "scripts/check_spelling.py"], "timeout": 120},
  "pyflakes": {"command": ["python3", "-m", "pyflakes", "src/python"]},
  "gate": {"command": ["..."]}
},
"queue": {"preflight": ["spelling", "pyflakes"]}
```

- **Each is one command.** It exits 0 to pass, 1 when it finds something, and anything else when
  it could not run. It prints each finding as `PATH:LINE: message`. It cannot be a lane group,
  a foreground, whole-host or self lane, or the gate. Without a `timeout`, it stops after 300 s.
- **`qk land` runs them** on the entry replayed onto `origin/main`, without admission, and
  refuses the entry on the first finding. A finding `origin/main` has too is not the entry's
  fault, so the entry is queued.
- **The worker runs them first** in every round, without admission, so a few seconds of
  checking never wait behind a gate. One that runs longer than a minute runs again admitted.
- **`qk preflight` runs them now** on your working tree, uncommitted changes included.
  `qk preflight LANE` runs one. It exits 0 when all pass, 1 on a finding, and 3 when one could
  not run.
- **`"queue": {"preflight": []}` runs none.**

A lane's command finds the project's main checkout in `QUEUEKEEPER_PROJECT_CHECKOUT` wherever it
runs, so a launcher can find tools that sit beside the project.

Until a project sets `queue.preflight`, the queue runs its lane named `spelling`, or else
Queuekeeper's built-in spelling check. A later release removes that fallback
([vocaro/queuekeeper#69](https://github.com/vocaro/queuekeeper/issues/69)).

### When a batch fails

The worker tries the cheap explanations before the expensive one:

1. **A flaky test.** Each failed test is rerun alone, up to three times. If every rerun passes
   and no commit in the batch touches the test's files, the batch lands. The flake is recorded in
   the queue's status, in a `Flaky-test:` trailer on the landed commit, and on the issue tracker.
   A test the batch itself adds is never treated as a flake.
2. **One obvious culprit.** If every failed test points at the same entry, that entry is sent
   back and the rest gate again at once.
3. **Bisect.** Otherwise the batch is halved until one entry fails alone. Both halves gate at the
   same time on different Macs, and every entry that passes along the way lands.

Steps 1 and 2 read test names from `qk unittest`, the bundled Python test runner. Any other lane,
including Xcode and Unity lanes for now, gets step 1 only if it declares `failedTests`, and never
step 2:

```json
"gate": {
  "command": ["scripts/test.sh"],
  "failedTests": {
    "results": "junit.xml",
    "rerun": ["scripts/test.sh", "--only", "{test}"],
    "paths": ["src/", "tests/", "scripts/test.sh"]
  }
}
```

- `results` is a JUnit XML file the lane writes in the folder `QUEUEKEEPER_RESULTS_DIR` names.
  Each failed `testcase` is a failed test, named `classname.name`.
- `rerun` runs one test alone, with `{test}` replaced by its name.
- `paths` lists the files the tests depend on (a directory ends in `/`). A batch lands on passing
  reruns only if it changes none of them.

A `qk unittest` gate run with `--fail-fast` stops once a failed test fails again alone, and runs
the recently failed test classes first, so a failing batch usually reaches these steps well before
the end of the suite ([Parallel tests](guides/parallel-tests.md#tests-within-one-suite)). Its
failed tests are rerun and blamed as any run's. If they then all pass alone, the batch does not
land, because the rest of its suite never ran: it gates again, once for that commit, and bisects
if the gate stops that way again.

An entry that is sent back is told the failed tests, whether its commits touch their files, and
the Mac's load during the run. When a preflight or docs lane failed, it is also told whether
`origin/main` fails that lane on its own, so the fix may belong on `main`.

### While a gate runs

- **Stacked gates.** Entries that arrive during a gate are tested on top of the batch, on a
  different Mac. If the batch lands, the stacked gate becomes the next round's gate, already under
  way. If not, it is thrown away. Only one runs at a time.
- **Documentation does not wait for code.** A documentation-only entry lands beside a running
  code gate.
- **Removing an entry** stops its gate within seconds and frees the Mac.

### No verdict is not a failure

A lane ends as **passed**, **failed** or **unmeasured**. A timeout, a lost connection, a Mac too
busy to admit the lane, a misconfigured Mac or a screen that locked during a UI test are all
unmeasured: they say nothing about the commits, so nobody's entry is sent back. The worker waits
for the Mac to settle and runs the lane again.

After three runs in a row without a verdict, the queue *holds* with every entry still waiting
([Holds](#holds)). An unreachable remote is retried for about an hour before the queue holds, and
during a GitHub incident the worker keeps trying while GitHub reports Git operations as degraded.

### Documentation changes

An entry that changes only documentation skips the code gate. It lands in seconds on the
preflight lanes and the documentation lanes, beside any code gate that is running.

- **Documentation** is Markdown in the usual places: the root `README.md`, `CONTRIBUTING.md` and
  `CHANGELOG.md`, and `.md` or `.markdown` files under `doc/` and `docs/`. A project names other
  paths with `queue.unread` on `main`.
- **Everything else is code**, including `AGENTS.md`, configuration, stylesheets, scripts, and
  images a document shows. One code path makes the whole entry ride the code gate, with the
  documentation lanes run ahead of it.
- **`qk land` says which gate an entry will ride**, after the `queued ID` line. For an entry that
  rides the code gate, it names the first path that is not documentation.
- **Land unrelated document changes on their own.** Commit them on a separate branch from
  `origin/main` so they land without waiting for code. Keep them in one entry when the document
  describes what the code change does.

The full rule is in [the merge queue guide](guides/merge-queue.md#the-merge-queue), under
*A docs-only batch runs the docs lanes*.

### Changes that build on each other

Queue dependent pieces together, in order, instead of landing and waiting for each in turn:

1. Give each piece its own branch, holding the pieces before it.
2. Run `qk land BRANCH` for each, oldest first, without waiting in between. The branches need
   different names, since landing a branch again replaces its earlier entry.
3. Follow them all with `qk queue wait ID ID ...`, which exits 0 when each has landed.

They usually ride one gate. Each piece lands once, as its own commit. If an earlier piece comes
back, the later pieces that hold its commits come back with it.

### Emergencies

- `qk land --front` tests an entry next, ahead of everything waiting.
- `qk land --no-gate --why "..."` lands with only the preflight lanes and the pre-push hook. The
  reason is required and recorded in a `Landed-without-gate:` trailer, and status lists the
  commit as ungated until a later gate covers it.
- With both flags, the entry lands next. It overtakes a round whose gate is still waiting for
  admission; a gate that has started finishes first.

Running the same flags again on an entry already queued updates it.

### Closing a queue for maintenance

Maintenance that needs an empty queue, such as moving its state or taking a Mac away, closes the
queue first. Checking that the queue is idle and then acting on it would leave a gap where a new
entry could arrive.

```sh
qk queue close --why "moving the queue's state" && {  # refuse new entries from here on
  until qk queue status --idle; do sleep 30; done     # let the worker finish what it has
  # ... the maintenance ...
  qk queue reopen
}
```

- **A closed queue refuses new entries** with
  `COMMIT is not queued: the queue is closed to new entries by WHO since WHEN: WHY` and exit 1.
- **Entries already queued still reach their verdict.** Closing is not a hold: `qk queue resume`
  leaves a closed queue closed, and `qk queue reopen` clears no hold.
- **`qk queue status` shows the closure** on its second line, and in `--json` as `closed`, which
  is null while the queue is open.
- **Whoever closed the queue reopens it.** A session whose entry was refused waits for the
  reopening and lands again.

### Entry names

Entry IDs look like `qk7318-issue-123-fix-share-sheet`: `qk`, a random four-digit number, an
`issue-N-` prefix when the branch or commits reference an issue, and a slug of the branch name
(without an `agent/` or `codex/` prefix) or of the commit subject.

## Lanes and admission

### Lanes

A lane is a named command in `queuekeeper.json`, with a timeout and a statement of what it needs
from the Mac. `qk run LANE` runs one, for an agent or for the queue.

```json
{
  "schemaVersion": 1,
  "name": "my-app",
  "extensions": [],
  "lanes": {
    "unit":    {"command": ["qk", "unittest", "-s", "tests"], "timeout": 1800},
    "ui":      {"command": ["scripts/ui-tests.sh"], "timeout": 3600, "unlockedScreen": true},
    "measure": {"command": ["scripts/benchmarks.sh"], "resource": "exclusive"},
    "gate":    {"parallel": ["unit", "ui"], "unlockedScreen": true, "budgetMinutes": 20}
  },
  "queue": {"lanes": ["gate"]}
}
```

The most useful settings:

| Setting | What it does |
|---|---|
| `timeout` | Seconds before the lane ends as unmeasured |
| `parallel` | Makes the lane a *group* that runs its members side by side under one admission |
| `resource` | `"exclusive"` for timing measurements that need the Mac to themselves; `"self"` for a script that admits its own parts |
| `wholeHost` | The lane starts its own parallel jobs and needs the whole Mac |
| `unlockedScreen` | Runs only on a Mac whose screen is unlocked ([Screen lock](#screen-lock)) |
| `labels` | Runs only on a Mac whose settings give it every label, such as `["unity-license"]` ([what a Mac takes](guides/another-mac.md#what-a-mac-takes)) |
| `minimumTests` | Rarely needed: a typed floor beside the [count from history](guides/lanes.md#test-counts-from-history), which already fails a whole run that executes fewer tests than the lane's last pass on main |
| `exactTests` | With `minimumTests`, a whole run must execute exactly that many, for a lane that picks its tests by name |
| `arguments` | `false` refuses `qk run LANE -- ARGUMENTS` for a lane that selects its own tests |
| `budgetMinutes` | Warns when the gate's median time drifts past this target, naming the slowest parts |
| `failedTests` | Lets the queue rerun a non-Python lane's failed tests ([When a batch fails](#when-a-batch-fails)) |

[`schemas/queuekeeper.schema.json`](../schemas/queuekeeper.schema.json) lists every setting.

- **Receipts.** Each run leaves a log and a receipt with the commit, command, exit status,
  outcome, Mac and wait time. Results of whole-lane runs on a clean commit are kept by commit, so
  any worktree can ask whether that commit already passed a lane.
- **Test counts** are read from unittest, pytest, XCTest and Swift Testing output
  ([test counts](guides/lanes.md#receipts-and-test-counts)).
- **Arguments.** `qk run LANE -- ARGUMENTS` appends arguments to the lane's command, to run some
  of its tests.
- **Groups.** A member that fails fails the group. A member that did not run, or ended
  unmeasured, leaves the group without a verdict rather than failed.

### Admission

Ten agents that each start a build at once make every build slow and every timing meaningless.
So every lane, and every command run through `qk admit -- COMMAND`, first asks the Mac for a
*slot*, in one first-come, first-served queue per Mac.

- **Slots.** Three slots are guaranteed. More, up to half the cores, open one at a time while the
  CPU is under the Mac's limit (90% by default, 70% for the Mac setup ran on) and memory pressure
  is normal.
- **Bigger requests.** A lane can ask for the whole Mac, for work that runs its own parallel
  jobs, or for exclusive use, for timing measurements. Exclusive work waits up to five minutes
  for the CPU to read quiet before it starts. One lane at a time may use an on-device model or
  the GPU.
- **Waiting says why.** A waiting lane says what it is waiting for and behind whom, every five
  minutes. `qk admissions now` shows who holds and who waits on every Mac, and
  `qk admissions waits` how often and how long lanes waited, for an unlocked screen as well as a
  slot, from each Mac's [wait history](guides/admission.md#wait-history).
- **Limits.** Admission never stops running work, and it cannot see load that never asks, such
  as an editor indexing in the background.

Don't wait for a quiet Mac before starting work, and don't poll `uptime` or `top`: admission
waits for a slot and chooses the Mac. [Scheduling](guides/scheduling.md) explains the rules and
the reasons behind them; [Heavy-work admission](guides/admission.md#heavy-work-admission) has
every detail.

### Process supervision

Each lane runs in its own process group with a time limit. When it ends, Queuekeeper stops
anything it left running. If something survives, the run is marked uncertain and its slot stays
held until that is settled, so leftover load is never hidden.

## The build farm

The build farm is the Mac you work on plus any other Mac you can reach over SSH.

### Adding and removing Macs

```sh
qk farm add DESTINATION [--name NAME]   # add a Mac, last in the order
qk farm list                            # the Macs, in the order work is offered to them
qk farm rename NAME NEW
qk farm pause NAME [--reason WHY]       # offer it no work until `qk farm resume NAME`
qk farm remove NAME                     # take it out for good
qk farm foreground NAME [--off]         # allow lanes that take its screen, or not
```

- A Mac's history follows its hardware, not its name, so renaming it is safe.
- A paused Mac stays listed and finishes what it is running.
- Removal is refused while work this Mac sent is still running there. Its checkouts stay on it
  until you delete them.

### Which Mac runs a lane

Each lane runs on the Mac with room for it and the most *headroom*: how far its CPU is under that
Mac's own limit. Your Mac is a candidate like any other, and the farm's order breaks ties. When
no Mac has room, the lane runs on whichever admits it first. Queuekeeper decides, not the agent.

- **Each Mac's limit is its own.** Because your Mac's limit is 70% and an added Mac's is 90%, the
  added Mac takes the work at equal load. Change a Mac's `busyLimit` to load it more or less.
- **An unreachable Mac** is skipped. **A misconfigured Mac**, with a different Xcode build for
  example, runs nothing and says so, rather than producing results that can't be compared.
- **Exclusive work** sent to another Mac waits for a moment it can start at once, for up to 15
  minutes, before it joins that Mac's queue.

### How the code gets there

The exact commit, or a snapshot of the working tree, is pushed over SSH straight to the other Mac;
nothing goes through the Git host. Each Mac keeps four checkouts per project and reuses the
closest one, so builds stay warm. **Environment variables do not travel**, so lane settings must
be arguments.

A run is journaled on the Mac that executes it. If your Mac sleeps or the connection drops, the
run continues, and your Mac collects the result later.

### Asking whether a command runs on another Mac

A command Queuekeeper sent to another Mac finds `QUEUEKEEPER_DISPATCHED_FROM` in its environment,
holding the sending Mac's name. A command running where it started does not. Test only whether
the variable is set:

```sh
if [ -n "${QUEUEKEEPER_DISPATCHED_FROM:-}" ]; then
    echo "sent here by another Mac"
fi
```

Don't compare host names, user IDs or the variable's value: macOS renames a Mac whose name
another machine claims, and a check written for the old name then fails without saying why.

### Screen lock

UI tests fail in confusing ways behind a locked screen. A lane marked `"unlockedScreen": true`
runs only on a Mac whose screen is unlocked, and waits while none is. A run that ended unmeasured
because the screen locked is not held against the commits. Queuekeeper cannot unlock a screen.

### macOS Local Network privacy

macOS can silently deny local-network access to a background process whose parent has exited.
Queuekeeper detects this rather than reporting the other Mac as down. The
[signed queue launcher](guides/maintenance.md#signed-queue-launcher) avoids it.

The [Apple plugin](../plugins/apple/src/queuekeeper_apple/README.md) keeps Xcode and simulator
runtimes alike across Macs.

## For agents

Most of Queuekeeper's users are AI agents, so its output is written to be acted on without a
person reading it. Nothing prompts: commands are meant to run in the background and be followed
to their result.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Passed or landed; for `queue result` and `queue logs`, the read succeeded |
| 1 | Failed or came back; for `queue logs`, the log is unavailable or the cursor is stale |
| 2 | Usage or configuration error |
| 3 | Unmeasured |
| 4 | Failed before any test started |
| 70 | Queuekeeper itself could not start (a plugin or extension did not load), so nothing ran |
| 75 | Busy, held, or the wait reached its limit; from `qk queue status --idle`, the queue is not idle |
| 78 | A Mac is misconfigured |
| 127 | A lane's command could not start |

### Output

- **Every run ends with fixed status lines**: the verdict, test counts, up to twenty failing
  tests with file and line, and the path of the full log. A run that failed without naming a test
  also quotes its command's last lines.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `land`, `queue add`, `queue wait`, `queue result`, `queue status`,
  `queue logs`, `queue doctor`, `queue health`, `queue placement`, `queue events`,
  `admissions now` and `doctor`.
- **`qk queue status --idle`** answers by exit status: 0 only when no worker runs and nothing is
  queued, testing or passed, and 75 otherwise.

### Following a landing

`qk land --wait` follows a commit to its result, so start it in the background and let its end
wake you.

- **While it waits**, it prints a line each time the entry's situation changes: waiting behind a
  round, gating on a Mac, halved in a bisect, passed and waiting for the push. When nothing
  changes, a `heartbeat:` line comes at most every five minutes.
- **A send-back ends the wait within seconds**, naming the conflicting files and entries, or the
  failed tests and the log.
- **An interrupted wait withdraws nothing.** The worker lands the entry whether or not anyone is
  waiting. Pick up the result with `qk queue wait ID`, which behaves like `qk land --wait`, or
  find it in `qk queue status`. Queue the commits again only after confirming the entry came back
  or was never queued.
- **Running `qk land BRANCH --wait` again** while the branch's entry is still queued adds nothing:
  it prints `ID already holds TIP (STATE)` and follows that entry. A branch with new commits
  replaces its earlier entry instead.
- **A refused entry because the queue is closed** gets `state: refused` and
  `reasonCode: "queue-closed"` in `--json`. Wait for the queue to reopen, then land again.
- **Each project lands in its own repository.** To land a commit in another project's queue, use
  `qk land --repo NAME BRANCH --wait`.
- **`qk queue status` shows the whole picture.** For a gate on another Mac, read the lane's own
  log there before calling a landing stuck.

### Structured results and recovery

`qk land --wait --json` prints one JSON result on standard output, with progress on standard
error. `qk queue add --json` returns the new entry at once, and `qk queue wait ID --json` follows
an existing one. Exit statuses are the same as in text mode.

`qk queue result ID --json` reads any entry, including old ones, without starting a worker. It
exits 0 whenever the lookup succeeds, so check `state` and `terminal` for the outcome (an unknown
ID exits 2). Use `queue wait` when the exit status must reflect the verdict.

An entry's result names its `commit`, `landedAs`, failure details, test counts, flaky tests
(`flakes`) and the run's artifacts. A missing or pruned artifact is listed with
`available: false`. Results carry `schemaVersion: 1`; tolerate added keys and unknown states.

Every result has a `recovery` object that says what to do next:

| Key | Meaning |
|---|---|
| `reasonCode` | A category such as `queue-held`, `wait-limit` or `entry-failed` |
| `responsibleParty` | `owner`, `session`, `unknown`, or null when no action is needed |
| `retryable` | Whether the same operation can simply be repeated |
| `action`, `message`, `suggestedCommands` | The next step, with commands as argument lists |

A held queue suggests a diagnosis, never a blind resume. An expired wait leaves the entry queued
and suggests following it again.

### Reading logs without losing earlier output

Agent tools often shorten terminal output, and `tail` hides the first failure. Keep the entry ID
and use `qk queue result ID --json` for the verdict and failure summary. To read the full log in
pieces, start at the beginning and pass each returned cursor back unchanged:

```sh
qk queue logs ID --json --limit 16384
qk queue logs ID --json --cursor CURSOR
```

- Each response has `content`, `nextCursor` and `hasMore`. Keep each chunk before moving on.
- A cursor fails with `log-cursor-stale` if the file is replaced or truncated; start over
  explicitly.
- `--artifact KIND` or `--artifact PATH` picks one of the entry's listed logs.
- A successful read is not evidence that the gate passed.

### Verifying

Every verification run goes through admission, so it takes a slot and shows in
`qk admissions now`:

- `qk unittest -s tests NAME...`, where NAME is a module, class or test;
- a lane, `qk run LANE -- FILTER`;
- any other command, `qk admit -- COMMAND`.

Never run `python3 -m unittest` directly, however small the run. It takes no slot, so other work
waits behind load nobody can see, and it misses `qk unittest`'s summary, logs, progress and the
suite's own setup. Only a test of a runner itself uses `--unadmitted`
([Heavy-work admission](guides/admission.md#heavy-work-admission)).

`qk run --expect N LANE -- FILTER` checks that exactly N tests *executed*. Skipped tests don't
count: if executed plus skipped make N, the run is unmeasured (exit 3), and any other count fails
(exit 2). The message says which N would count what ran.

### Long runs

Keep a long run's progress readable while it runs:

- **Write its output to a file**, `COMMAND > FILE 2>&1`, as a background command, and trim when
  reading. Never pipe a long run through `tail` or `head`, which print nothing until the end.
- **`qk unittest` keeps its own progress.** Its first line names the folder, under
  `.build/queuekeeper/runs/`, where `progress.log` gains a line as each test process ends.
- **`qk progress`** reports on a run from another shell: what it runs, how long, the tests
  finished and failed, and when it last moved. `qk progress FOLDER` reads one run, and `--all`
  reads every one kept.

`qk run` keeps each lane's log in `.build/queuekeeper/LANE/run.log`.

## Watch the queue

`qk queue status` says what is waiting and why. Three optional features show more. Each is off
until you turn it on, and each can be turned off without affecting the queue:

| Feature | What it gives you | Command |
|---|---|---|
| `watch` | A live view of the queue in the terminal | `qk watch` |
| `notify` | A banner, command or webhook when the queue needs you | `qk notify` |
| `dashboard` | A read-only web page of every queue and Mac | `qk serve` |

```sh
qk visibility status            # each feature, and whether it is on
qk visibility enable notify
qk visibility disable notify
```

These settings belong to you and your Mac, not a project, so they live in
`~/Library/Application Support/queuekeeper/visibility.json`. Setting
`QUEUEKEEPER_VISIBILITY=off` turns every feature off for commands that see it.

### The watch feature

`qk watch` redraws the queue in the terminal every two seconds: what it is doing, each running
gate, what waits, what settled recently and the latest events.

```sh
qk visibility enable watch
qk watch                  # live, until Ctrl-C
qk watch --once           # print once
qk watch --interval 5 --events 20
```

### The notify feature

Notify tells you when the queue needs you, without slowing the queue.

```sh
qk visibility enable notify
qk notify status          # each rule and channel, and whether it is on
qk notify test            # send a test notice through each channel
qk notify check           # look for a stuck queue now
```

**Rules** choose what you hear about:

| Rule | Tells you when | Default |
|---|---|---|
| `held-owner` | The queue stopped for a cause only you can fix | On |
| `held-session` | The queue stopped for a cause an agent fixes | Off |
| `came-back` | An entry failed its gate or conflicts with `main` | On |
| `stuck` | A gate is stuck or the queue's doctor finds the queue stuck | On |
| `flaky` | A failed test passed alone | On |
| `ungated` | An entry landed without the gate | On |
| `landed` | An entry landed | Off |
| `resumed` | A hold was cleared | Off |

**Channels** choose how:

| Channel | What it does | On when |
|---|---|---|
| `banner` | Posts a macOS notification | On a Mac, unless turned off |
| `command` | Runs your command with the notice as JSON on standard input | Its `argv` is set |
| `webhook` | Posts the notice as JSON (with a Slack-friendly `text` key) to your URL | Its `url` is set |

```sh
qk notify enable landed                     # a rule
qk notify disable banner                    # a channel
qk notify set webhook url https://example.com/hook
qk notify set command argv '["/usr/local/bin/tell-me", "--queue"]'
qk notify unset webhook url
```

Good to know:

- Each event is told at most once, and a burst of more than five notices is summarized.
- A failing channel never holds up the others; its errors go to `notify.log` beside
  `visibility.json`.
- Banners appear to come from Script Editor, so allow its notifications in System Settings.
- A stalled queue writes no event, so Queuekeeper looks for one, at most every two minutes,
  whenever a command such as `land` or `run` runs, or every two minutes with the signed launcher.
  A problem is reported once it has lasted five minutes.

### The dashboard

The dashboard is a read-only web page of every queue and Mac, at `http://localhost:8750/`.

```sh
qk visibility enable dashboard
qk serve --open           # serve, and open it in the browser
qk serve --port 9000
qk serve ../other-project # show another repository too
```

| Tab | What it shows |
|---|---|
| Now | One health verdict across every queue, what needs attention, the day's numbers, a Flow map of entries moving from agents through the queues and Macs to `main`, and every round being tested and entry waiting |
| Farm | Each Mac's slots, load, memory pressure and screen lock, with what runs and waits there; device reservations and connections |
| Trends | The last 7 or 30 days: landings per day, how often entries came back, gate times and flaky tests |
| Activity | Each round, newest first, with how each entry ended and links to every lane's log |
| Logs | A log with its failures marked and its passing suites folded; reached from any lane, failure or entry |

- It shows every repository whose queue has run while the feature was on. The picker in the
  header narrows any tab to one.
- It only reads: it has no buttons that change the queue.
- It listens on `127.0.0.1` only. `--bind ADDRESS` serves another address, with no sign-in, so
  anyone who can reach it can read the queue and its logs.
- Serving again replaces a dashboard already on the port; `--no-replace` refuses instead.

### The event journal

Every change of queue state is appended to a journal in the repository's Git directory, which
the features above read. You can read it too:

```sh
qk queue events                       # everything, oldest first
qk queue events --follow              # then each new event as it happens
qk queue events --since 2026-11-12
qk queue events --kind entry.         # kinds that begin with entry.
qk queue events --json
```

The [developer guide](developer_guide.md#the-event-journal) lists the event kinds and their keys.

## Troubleshooting

### Holds

When the queue stops, it *holds* with every entry still waiting, and the hold's reason says whom
it is for:

- **Most holds are for an agent**, which fixes the cause and runs `qk queue resume`.
- **A hold is for the owner** only when an agent cannot fix the cause, such as a refused SSH key.
  With the [notify feature](#the-notify-feature) on, you hear about these at once.

Common holds:

- **No verdict three times in a row** ([No verdict is not a failure](#no-verdict-is-not-a-failure)).
- **Queuekeeper itself could not start** in a lane (exit 70). No commit was tested. Bring
  Queuekeeper and the folders the project's extensions load from to versions that work together.
- **An extension folder beside the project.** A run loads such a folder at its `origin/main`, not
  as your checkout stands. Test a change to it with `qk run --sibling FOLDER=WHERE LANE`, and land
  it before the change that needs it
  ([Extension folders beside a project](guides/another-mac.md#extension-folders-beside-a-project)).

### A queue that looks stuck

- **`qk queue doctor`** reads the queue's records and says whether it is testing or stuck, and
  what would unstick it. It names a gate that has waited 15 minutes for admission and what holds
  the slots, and any Mac short of memory with its largest processes. It stops nothing itself.
- **`qk queue health`** gives one verdict on whether the queue is healthy and when the waiting
  entries should land, from five signs: progress of running gates, time since the last landing,
  each Mac's reachability and memory, connected devices, and how many recent entries came back.
  It exits 0 when healthy or quiet, 1 when it needs attention, and 3 when it can't tell. The
  dashboard shows the same verdict.
- **`qk queue placement`** shows whether each Mac could start a gate now.
- **`qk admissions now`** shows who holds and who waits for slots on every Mac.

### Checking a project's setup

`qk doctor` checks a project's lanes and every Mac's setup. It also notes recent Python crash
reports on each Mac, without changing its verdict.

### Disk space

`qk disk` lists Queuekeeper's build caches, test results and simulator resources, with what is
safe to delete ([Reclaiming disk space](guides/disk-cleanup.md#reclaiming-disk-space)).

## Further reading

- [Related tools](related_tools.md) compares Queuekeeper with other merge queues.
- The plugins: [Apple](../plugins/apple/src/queuekeeper_apple/README.md),
  [Unity](../plugins/unity/src/queuekeeper_unity/README.md),
  [Mac desktop](../plugins/mac-desktop/src/queuekeeper_mac_desktop/README.md) and
  [Mac hardware](../plugins/mac-hardware/src/queuekeeper_mac_hardware/README.md).
- The [developer guide](developer_guide.md) and its [reference guides](developer_guide.md#reference-guides)
  for the detail behind every rule.
