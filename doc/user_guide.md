# Queuekeeper user guide

Everything you need to set up Queuekeeper, land changes through it, and follow what it is doing.
The [README](../README.md) explains what Queuekeeper is for. The [developer guide](developer_guide.md)
covers how it works inside and how to change it.

- [Set up](#set-up): requirements, install, configure a project, tell your agents, add Macs
- [Land changes](#land-changes): what the merge queue does with a commit
- [Lanes and admission](#lanes-and-admission): the commands Queuekeeper runs, and when a Mac has room
- [The build farm](#the-build-farm): running work on more than one Mac
- [Apps, devices and simulators](#apps-devices-and-simulators): physical devices, the simulator pool,
  foreground runs, kept app builds, Unity editors
- [For agents](#for-agents): exit codes, structured results, logs and verification
- [Watch the queue](#watch-the-queue): the terminal view, notifications and the dashboard
- [Troubleshooting](#troubleshooting): holds, a stuck queue, checking a project's setup
- [Disk space and leftovers](#disk-space-and-leftovers): what `qk disk` finds and deletes

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

```json queuekeeper.json
{
  "schemaVersion": 1,
  "name": "my-project",
  "plugins": [],
  "lanes": {
    "gate": {"command": ["scripts/test.sh"], "timeout": 1800}
  }
}
```

| Key | Meaning |
|---|---|
| `schemaVersion` | Always `1` |
| `name` | The project's name |
| `plugins` | The [plugins](../README.md#plugins) to load. `[]`, or leaving it out, loads the core alone |
| `disk` | `{"startFreeGB": N}`, the free space a lane that fills the disk waits for ([Free disk space](#free-disk-space)) |
| `command` | An argument list, not a shell string |
| `timeout` | Seconds. A lane that reaches it ends as *unmeasured*, not failed |

The queue always reads its settings from `main`, so a branch cannot loosen its own gate. The one
exception is the commit that adds `queuekeeper.json`, which is gated by its own file since `main`
has none yet ([A project's first landing](#a-projects-first-landing)).

Then check the setup:

```sh
qk doctor
```

It reports missing commands, lanes without a timeout, tests that no lane runs, and problems on
any Mac.

Two defaults to know about:

- **Preflight lanes.** List the project's cheap checks, such as a linter, in `queue.preflight`.
  The queue runs them before it accepts an entry and first in every round
  ([Preflight lanes](#preflight-lanes)). Without it, or with `"queue": {"preflight": []}`, there
  are none. Queuekeeper has no spelling check of its own.
- **Documentation-only changes** skip the gate and run the documentation lanes instead
  ([Documentation changes](#documentation-changes)). Set `"queue": {"unread": false}` to gate
  every change.
- **Every gate lane runs for every code change** unless the gate lanes list their `inputs`
  ([Gate lanes chosen by changed paths](#gate-lanes-chosen-by-changed-paths)).

[Lanes and admission](#lanes-and-admission) describes the other lane settings.

### Tell your agents

Agents have to be told to queue their work instead of pushing it. In the project, run:

```sh
qk agents setup --guard
```

It writes the rule below into `AGENTS.md`, or `CLAUDE.md` when only that exists (`--file PATH`
picks others), and merges [Claude Code's hooks](#claude-codes-hooks) into `.claude/settings.json`,
leaving everything else in both files as it was. Without `--guard` it adds only the session-start
hook. Commit both files and land them, so that every worktree has them.

- **Running it again** brings the rule up to date with this version of Queuekeeper. The rule sits
  between two marker lines, and the first records a hash of what was written: a block someone
  edited since is left alone, and `--force` replaces it.
- **`qk agents remove`** takes out the block, unless it was edited, and only the hooks setup
  adds. A file left empty is deleted.
- **`qk agents check`** says what is in place, and exits 0 when the rule and the session-start
  hook both are, and no hook runs its launcher by a relative path.
- **`--launcher`** names the command the hooks run, `qk` unless you say otherwise, for example
  `--launcher "python3 scripts/qk"`. Claude Code runs a hook wherever the session has `cd`'d to,
  so setup anchors a path relative to the project on `$CLAUDE_PROJECT_DIR`: that example is
  written as `python3 "$CLAUDE_PROJECT_DIR"/scripts/qk`. Running setup again anchors a hook an
  earlier version wrote with a relative path.

For other agents, add the rule to the instructions they read yourself, adapting the lane names:

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
This works in a repository created empty too, with no `main` yet: that landing creates `main`
([A project's first landing](#a-projects-first-landing)).

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

From then on each lane runs on whichever Mac has room. [Setting up a Mac](#setting-up-a-mac) has
the full checklist, including Xcode, simulator runtimes, private sources and signing.

### Keep the queue running

A queue worker starts by itself when you run `qk land` and exits when nothing is waiting. To also
check for waiting work at login and every two minutes, set up the signed queue launcher below. It
also avoids a macOS restriction that can cut a background worker off from the local network
([macOS Local Network privacy](#macos-local-network-privacy)).

#### The signed queue launcher

Set this up once, on the Mac that owns the queues. From the Queuekeeper checkout, build the app
and sign it in your own Terminal with an Apple-issued signing identity:

```sh
app="$PWD/.build/Queuekeeper.app"
mkdir -p "$app/Contents/MacOS"
cp src/python/queuekeeper/_native/queue-launcher/Info.plist "$app/Contents/Info.plist"
python3 scripts/qk admit --here -- /usr/bin/clang -std=c11 -O2 -Wall -Wextra -Werror \
  src/python/queuekeeper/_native/queue-launcher/main.c -o "$app/Contents/MacOS/QueueLauncher"
/usr/bin/codesign --force --sign "Developer ID Application" --timestamp=none "$app"
/usr/bin/codesign --verify --strict "$app"
```

Install it at a stable path and register it with Launch Services. When macOS asks, allow
Queuekeeper Local Network access; if it is later blocked from reaching another Mac, check System
Settings, Privacy & Security, Local Network.

```sh
mkdir -p "$HOME/Applications"
/usr/bin/ditto "$app" "$HOME/Applications/Queuekeeper.app"
app="$HOME/Applications/Queuekeeper.app"
/usr/bin/open -g -a "$app" --args /usr/bin/true
```

Then, for each queue, run this from its main checkout with the Python and `PATH` its worker should
use after login. The worker uses the account's normal Git and SSH credentials.

```sh
draft="$(mktemp)"
python3 scripts/qk queue launcher-plist "$app" > "$draft" &&
  label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$draft")" &&
  mkdir -p "$HOME/Library/LaunchAgents" &&
  install -m 644 "$draft" "$HOME/Library/LaunchAgents/$label.plist" &&
  /bin/launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
rm -f "$draft"
```

`qk land`, `qk queue wait` and `qk queue resume` ask launchd to start the agent, which runs the
same worker at login and on its interval. An empty or held queue returns at once, and the queue's
lock keeps out a second worker. If launchd refuses a start, `worker.log` records why and the
session starts the worker itself.

#### Removing the launcher

Close the queue, wait until it is idle, then boot the agent out and remove its plist:

```sh
python3 scripts/qk queue close --why "removing the queue's LaunchAgent" --for 2h && {
  until python3 scripts/qk queue status --idle; do sleep 30; done &&
    /bin/launchctl bootout "gui/$(id -u)/$label" &&
    rm "$HOME/Library/LaunchAgents/$label.plist"
  python3 scripts/qk queue reopen
}
```

- **Wait on `--idle`, not plain status.** The bootout ends a running worker and its gate.
  `qk queue status --idle` exits 0 only when nothing is queued, testing or passed and no worker
  runs, or the one that runs only waits to move the shared checkout, and 75 otherwise; plain
  `qk queue status` exits 0 mid-gate. Ending a worker that only waits ends no gate: the next
  worker moves the checkout.
- **Close first**, so no entry arrives between the idle reading and the bootout
  ([Closing a queue for maintenance](#closing-a-queue-for-maintenance)). Reopen afterward whether
  or not the removal worked, if this `close` was the one that closed the queue.
- **A held queue with entries never goes idle.** Resume it, or remove its entries, first.

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
qk queue remove ID          # take an entry out; --why TEXT tells its session why
qk queue inbox              # this worktree's or branch's outcomes nobody has confirmed
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

A push can also be cut off before its answer comes back, by a timeout, a dropped connection or
the worker's death, after the remote took it. Such an entry is **publication-unverified**: it
stays waiting, and nothing is pushed or gated for it until a fetch of `origin/main` says whether
the push landed. The worker's next round checks. If `main`'s history holds the pushed commit, even
with more landings after it, the entry lands as that commit. If not, the passed batch is pushed
again, or gated again when it is no longer the batch that passed. Don't queue such an entry again.

### Checks before queuing

`qk land` refuses an entry within seconds, with nothing queued, when a round would only send it
back later. `--front` and `--no-gate` skip none of these checks.

- **A branch built on `main`'s history from before a rewrite**, such as one `git filter-repo`
  makes. Such a branch carries old copies of commits `main` has under new IDs, with the same
  dates and messages. Replaying them would put back anything the rewrite removed, so three or
  more such copies refuse it. The refusal gives `git rebase --onto origin/main COPY BRANCH`, where
  COPY is the newest old copy, to move the branch's own commits alone. It also lists any commit
  among the old history that `main` has in no form: one the rewrite changed or dropped, or one of
  yours to cherry-pick onto the result. A branch that still holds commits `main` landed as a
  replay or a cherry-pick is not refused, since a replay gives each commit a new committer date.
- **Conflicts** with `origin/main` or any entry ahead. `qk land` replays the entry the way a round
  would, after every entry ahead, in a scratch worktree. The refusal names the files and your own
  lines in conflict (`PATH:FIRST-LAST`) and the entries ahead that change those files.
  - A conflict with entries ahead only: `qk land --wait` waits for them to land and tries again
    by itself. Without `--wait`, the refusal gives a `qk queue wait ID ID ...` command to run in
    the background; it exits 0 when they all landed (rebase and land again), 1 when one came back
    (land again), and 75 while the queue is held.
  - A conflict with `origin/main` is refused at once, since only a rebase ends it.
  - Lines several branches each add below one heading of a `CHANGELOG.md` are not a conflict: the
    worker keeps both, the new lines first.
- **Commit messages that close an issue by mistake**, such as "This does not fix #43", which
  GitHub reads as closing issue 43. The refusal names the line and what to write instead.
- **Tests that cannot run**, such as a test module that does not parse, in any queue lane that
  runs `qk unittest`.
- **Preflight findings** ([Preflight lanes](#preflight-lanes)).

A refusal is one line, `COMMIT is not queued: REASON`, with exit status 1. A check that cannot be
made queues the entry anyway and says so; the round checks again. A branch refreshed by merging
`main` is not refused; the queue lands its own commits.

### Add-only JSON objects

A project can let the queue combine independent key additions to a JSON object. Set
`queue.addOnlyJson` in `queuekeeper.json` and land that setting first. Each entry names a literal
repository-relative JSON file and the object keys leading to the selected object. An empty array
selects the root object of the file.

```json
{
  "queue": {
    "addOnlyJson": {
      "scripts/test-classes.json": ["classes"],
      "data/ids.json": []
    }
  }
}
```

Both sides must preserve existing keys and values and leave everything outside the selected object
unchanged. New keys from both sides are kept, and a key with identical new values is kept once.
When two entries add the same key with different values, the conflict names the key, even if the
additions appear on different lines. Git conflicts involving edits, removals or malformed JSON
still need a manual resolution. This setting does not change which gate lanes run.

### Branches after a conflict or a landing

- **The first commit that conflicts is the one to fix.** The queue picks an entry's commits one at
  a time, so a branch whose final tree would apply is still refused when an earlier commit does
  not. If the commits from that one on are yours alone and pushed nowhere, squash or amend them
  so each applies in order. Otherwise wait for the entry ahead to land and rebase onto
  `origin/main`.
- **Start the next change from `origin/main`** once a landing says `landed`. The queue may change
  a commit as it lands it, as when it settles a changelog conflict, so `main` holds it under
  another hash. A branch that goes on after a landing still holds the old commits; rebase it onto
  `origin/main`, using `git rebase --skip` for a commit whose change `main` already has. Never
  merge `main` in.
- **A landed branch is deleted for you** when the queue can prove it landed. `qk land --wait`
  does it for its own branch once the entry lands, and `qk queue reclaim` does it for every branch
  the queue holds a landed entry from ([Cleaning up after a landing](#cleaning-up-after-a-landing)).

### Preflight lanes

Preflight lanes are a project's cheap checks: a spelling check, a linter, a formatter in check
mode. The project names them, in order, in `queue.preflight` on `main`:

```json
"lanes": {
  "spelling": {"command": ["python3", "scripts/eng", "spelling"], "timeout": 120},
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
- **`"queue": {"preflight": []}`, or no `queue.preflight`, runs none.**
- **Only `main`'s list counts.** Removing a lane and what it reads takes two landings
  ([Changes main's copy judges](#changes-mains-copy-judges)).

A lane's command finds the project's main checkout in `QUEUEKEEPER_PROJECT_CHECKOUT` wherever it
runs, so a launcher can find tools that sit beside the project.

Queuekeeper has no spelling check of its own, and runs no lane first because of its name. A
project that wants one brings its own, such as vocaro-engineering's `eng spelling` above
([vocaro/queuekeeper#69](https://github.com/vocaro/queuekeeper/issues/69)).

### When a batch fails

The worker tries the cheap explanations before the expensive one:

1. **A flaky test.** Each failed test is rerun alone, up to three times. If every rerun passes
   and no commit in the batch touches the test's module or a file it imports, the batch lands. The
   flake is recorded in the queue's status, in a `Flaky-test:` trailer on the landed commit, and on
   the issue tracker. A test the batch itself adds is never treated as a flake: the entry that adds
   it is sent back.
2. **One obvious culprit.** If every failed test points at the same entry, the worker confirms it
   by running those tests on the batch without that entry. If they pass, that entry is sent back
   and the rest gate again at once.
3. **Bisect.** Otherwise the batch is halved until one entry fails alone. Both halves gate at the
   same time on different Macs, and every entry that passes along the way lands.
4. **Entries that fail together.** When both halves pass, no entry fails alone: some of them break
   each other. The worker gates the whole set again, then smaller subsets of it one at a time,
   dropping entries while the rest still fail, until it has the fewest that fail together, and
   gates each of those alone. Each comes back naming the others by entry ID and commit, and the
   rest gate again. An entry that fails alone after all comes back alone, as from a bisect. Two
   entries that fail together cost one extra gate; a larger set at most about two per entry.

Steps 1 and 2 read test names from `qk unittest`, the bundled Python test runner, and are skipped
when more than five tests failed. A lane whose command is `qk unity test` gets step 1 from the
[Unity plugin](../plugins/unity/src/queuekeeper_unity/README.md#flaky-tests), and never step 2.
Any other lane, including Xcode lanes for now, gets step 1 only if it declares `failedTests`, and
never step 2:

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
  reruns only if it changes none of them. With none listed, the flakes are recorded and the batch
  is bisected.

A `qk unity test` lane's failed tests are named by the plugin from Unity's `results.xml` and rerun
with `qk unity test --filter TEST`, so its `failedTests` gives `paths` alone, such as
`"failedTests": {"paths": ["lab/Assets/", "lab/Packages/"]}`. Without it, the lane's flakes are
still recorded, and the batch is bisected.

A `qk unittest` gate run with `--fail-fast` stops once a failed test fails again alone, and runs
the recently failed test classes first, so a failing batch usually reaches these steps well before
the end of the suite ([Running tests side by side](#running-tests-side-by-side)). Its
failed tests are rerun and blamed as any run's. If they then all pass alone, the batch does not
land, because the rest of its suite never ran: it gates again, once for that commit, and bisects
if the gate stops that way again.

A gate group with `failFast` that stopped at its first failed member is treated the same way: when
that member's failed tests all pass alone, the batch gates again, once for that commit, and bisects
if the group stops that way again, since its other members never finished
([Lane groups](#lane-groups)).

A gate that `qk unittest` refused with `nothing ran` is not rerun: the worker sends back the one
entry that brings the refusal, or bisects. When the same refusal comes back with no entry to blame,
the queue holds for a session.

A lane that passes but changes the tracked files it tested fails too. After each lane, the worker
compares the checkout with the commit it built, both on this Mac and in another Mac's checkout
that ran the lane. Any edit, staged or not, a deletion, or a moved `HEAD` sends the batch back
like a failure, without reruns. The rejection names the lane and the changed files. The queue
pushes the commit it built, not the tree a lane leaves, so a lane that rewrites sources, such as
a formatter, would otherwise test one tree and land another. Files Git does not track, like
build products and results, don't count. Before sending anything back, the worker runs that lane
once more on `origin/main` without the batch. When `main` changes its own tree too, as a formatter
lane added to `main` that rewrites files `main` already has does, no entry is at fault: the queue
holds for a session, with every entry still waiting, and the hold names the lane and files. Queue a
change that leaves `main` as the lane leaves it, such as the lane's rewrite committed, then resume
the queue.

An entry that is sent back is told the failed tests, whether its commits touch their files, and
the Mac's load during the run. One that fails together with others is told their entry IDs and
commits in its reason and, in `--json`, in `conflictWith` (`[{"id": ..., "sha": ..., "state": ...}]`),
with `failure.category` `fails-together` and `recovery.reasonCode` `entry-fails-together`. A
partner whose `state` is `failed` came back too: rebase onto its commit (each entry's
`refs/queuekeeper/queue/ID` stays until it is removed), fix how the changes combine, and land the
result once; or wait for it to land and rebase onto `origin/main`. A partner whose `state` is
`landed` is already on `main`: rebase onto `origin/main` and fix how the changes combine. When a preflight or docs lane failed, it is also told whether
`origin/main` fails that lane on its own, so the fix may belong on `main`. The entry keeps its ref
until `qk queue remove ID`; fix it, rebase and land it again.

A failure in a test your change cannot reach, which the queue did not excuse as a flake, goes on
that test's issue before you land again, so the flake becomes somebody's to fix.

### While a gate runs

- **Stacked gates.** Entries that arrive during a gate are tested on top of the batch, on a
  different Mac, once the batch's last lane has been admitted. If the batch lands, the stacked gate
  becomes the next round's gate, already under way. Anything else (the batch fails or lands at
  another commit, an entry is removed or put first, `main`'s lanes change, the queue holds) throws
  it away and queues its entries again. Only one runs at a time.
- **Documentation does not wait for code.** A documentation-only entry lands beside a running
  code gate, in a checkout of its own, as long as it changes none of the gate's paths. Queued
  beside code, documentation lands first, in a round of its own.
- **Removing an entry** (`qk queue remove ID`, or landing its branch again) stops its gate within
  seconds on whichever Mac, frees the admission, and gates the rest again with no verdict counted.
- **After a failure**, `qk queue status` and `qk land --wait` follow it: the worker reading the
  failure, the failed tests waiting to rerun alone, then each rerun with its log and what its
  admission waits for.

### No verdict is not a failure

A lane ends as **passed**, **failed** or **unmeasured**. A timeout, a lost connection, a Mac too
busy to admit the lane, a misconfigured Mac, a screen that locked during a UI test or a build plan
that names a Metal Toolchain mount macOS has since replaced (see [The simulator pool](#the-simulator-pool)) are all
unmeasured. The worker waits for a Mac to settle (CPU under its busy limit and memory pressure
normal, for up to an hour) and runs the lanes again.

- **Three runs in a row without a verdict** send back the batch with the reason and the gate
  log. The worker records both `qk doctor --check-only` and `qk queue doctor` and continues with the queue.
  With the [notify feature](#the-notify-feature) on, the usual send-back notice tells the owner.
  A project changes the bound on `main` with `"queue": {"noVerdictRuns": N}`. A verdict or a
  change in the batch starts the count again. If no eligible Mac settles within an hour, the
  worker sends back the batch at that point.
- **A screen that locked during the run** has a separate count. The lane next runs only on an
  unlocked Mac. Five runs in a row that start unlocked and end locked send back the batch.
- **A lane whose command could not start** (exit 127), and **a lane Queuekeeper itself could not
  start** in (exit 70, a plugin that did not load), hold the queue at once, since a
  rerun would fail the same way. If the batch's own commits break loading, that entry is found by
  bisecting and sent back instead.
- **Running out of disk space** holds the queue at once. Free space with `qk disk --delete` and
  resume.
- **An unreachable remote** is retried for about an hour before the queue holds. A push refused
  for its key while fetches succeed is retried too, for up to an hour while GitHub reports Git
  operations as degraded.

### Documentation changes

An entry that changes only documentation skips the code gate. It lands in seconds on the
preflight lanes and the documentation lanes, beside any code gate that is running.

- **Documentation** is Markdown in the usual places: the root `README.md`, `CONTRIBUTING.md` and
  `CHANGELOG.md`, and `.md` or `.markdown` files under `doc/` and `docs/`.
- **Everything else is code**, including `AGENTS.md`, configuration, stylesheets, scripts, and
  images a document shows. One code path makes the whole entry ride the code gate, with the
  documentation lanes run ahead of it so a cheap failure comes back first.
- **`qk land` says which gate an entry will ride**, after the `queued ID` line. For an entry that
  rides the code gate, it names the first path that is not documentation.
- **Land unrelated document changes on their own.** Commit them on a separate branch from
  `origin/main` so they land without waiting for code. Keep them in one entry when the document
  describes what the code change does.
- **A docs-only landing is not a full gate** of the pushed commit, and it skips the pre-push hook,
  so put required documentation checks in the documentation lanes.

#### Paths no code lane reads

`queue.unread`, read only from `main`, decides two things with one list: whether a batch may skip
the gate (every path it changes is unread), and whether a passed result still stands when `main`
moves under it (every path `main` gained is unread and untouched by the batch).

```json
"queue": {
  "unread": {
    "paths": {"records/": "landing records; no lane reads them",
              "doc/release/": {"why": "only the privacy checks read it", "lanes": ["privacy-docs"]}},
    "exclude": {"doc/guide/input.md": "a code test reads this fixture"},
    "lanes": ["docs"]
  }
}
```

Every key is optional:

- `paths` adds to the Markdown defaults: a file, or a directory ending in `/` (which takes every
  file in it, not only Markdown), with why no code lane reads it. A path one or two small checks
  read can name those lanes instead, and a docs-only batch that changes it runs them too.
- `exclude` names paths code does read, and wins over both.
- `lanes`, by default `["docs"]`, is what a docs-only batch runs in place of the gate. The default
  `docs` lane is `qk docs check`.
- `"defaults": false` drops the Markdown defaults. `"unread": false` gates every batch and carries
  nothing over.

List a path only after checking that no lane reads it: a path left out costs a gate run, and a path
wrongly listed lands untested. A test that reads a document needs that document in `exclude`, or
a documentation lane that runs the test. The older `queue.docsOnly` and `queue.carryOver` are
refused, and the error prints the `queue.unread` to give in their place.

When `main` moved only by unread paths, a passed batch is pushed without gating again, after
`qk docs check` and any `queue.carryOverChecks`, commands run in order on the batch rebased onto the
new `main`:

```json
"queue": {
  "carryOverChecks": [
    {"command": ["python3", "tools/check_records.py", "--base", "{base}"], "paths": ["records/"]},
    {"command": ["python3", "scripts/qk", "docs", "check"]}
  ]
}
```

`paths` limits a costly check to moves of those paths, and `{base}` becomes the commit the result
passed on. A check that fails sends the batch through the gate again, with no blame. A path whose
`queue.unread.paths` entry names lanes never carries over, so a check limited to such paths never
runs, and `qk doctor` warns of it: name the check's lane on the path instead, and a batch that
changes the path runs it.

### Gate lanes chosen by changed paths

A project can run only the gate lanes a batch needs, and none when it needs none. Each gate lane,
one of `queue.lanes` or else `gate`, lists `inputs`: the globs of the paths that affect it.

```json
"lanes": {
  "unit": {"command": ["python3", "scripts/qk", "unittest", "-s", "tests"],
           "inputs": ["src/python/**", "tests/**", "schemas/*.json"]},
  "app":  {"command": ["xcodebuild", "test", "-scheme", "App"],
           "inputs": ["apple/**", "*.xcconfig", "schemas/*.json"]}
},
"queue": {"lanes": ["unit", "app"]}
```

- **A glob matches the whole path** from the repository root. `*` and `?` stay within one
  directory, and `**` spans any number of them. A moved file counts by both its old and new path.
- **A lane runs when a changed path matches one of its `inputs`.** A batch runs its preflight,
  provision and documentation lanes as usual, then the gate lanes its paths select, in
  `queue.lanes` order. `qk land` and the round's log say which lanes were selected.
- **A gate lane without `inputs` runs for every change.** A project can narrow one lane at a time,
  and nothing skips the gate until every gate lane lists its inputs.
- **A batch that selects no gate lane skips the gate.** It lands as a batch of unread paths does
  ([Documentation changes](#documentation-changes)): on its preflight lanes and `qk docs check`,
  or the lanes `queue.unread.lanes` names. `qk land` says
  `skips the code gate: no gate lane's inputs name PATH`.
- **`queuekeeper.json` and `queuekeeper-workspace.json` affect every lane**, whatever the globs
  say, since they hold the lanes' own commands.
- **`queue.unread` still says what code reads.** A path its `exclude` names, or any path when it is
  `false`, runs every gate lane unless a lane's `inputs` name it.
- **Only `main`'s `inputs` count**, so a change cannot excuse itself from a lane.
- **A landing says what its gate left out.** `qk queue status` shows
  `partial gate: of the gate lanes, the unit lane ran and the app lane did not`, and the entry's
  record and `qk queue result --json` carry `partialGate`: `lanes`, the gate lanes its paths
  selected, `skipped`, the others, and for a lane group `members`, with the group's lanes that
  `ran` and were `skipped`. It is what the paths called for: a bisect's half or a stacked gate may
  have run more. The `entry.landed` event carries it too.
- **A full run on `main` is the backstop.** Once the queue has drained, its worker gates
  `origin/main` whole, every gate lane and every lane of a group, when landings with a partial
  gate have had no whole gate since: five of them, or the oldest four hours old. A later landing
  whose own gate ran every lane covers the ones before it, so a busy project rarely needs the run.
  A pass records `fullRun` on each. A failure runs the failed lane at each of those landings in
  turn, oldest first, and holds the queue, naming the first at which it fails: its paths skipped
  that lane, so a lane's `inputs` leave out a path it reads. Land a fix, or the corrected `inputs`,
  and resume the queue. `"queue": {"fullRun": {"landings": 2, "minutes": 60}}` on `main` changes
  when it runs, and `"fullRun": false` turns it off, which leaves `qk doctor`'s list as the only
  guard.
- **`qk doctor` lists what skips.** Its `notes` count the tracked files that no gate lane's
  `inputs` and no `queue.unread` path names, and `affectedPaths.skipping` lists them all. A glob
  that matches no tracked file is a warning, listed in `affectedPaths.unmatched`.

List every path a lane reads: a file left off a lane's `inputs` lands untested by that lane, and
the next batch that runs the lane takes the blame. Read `qk doctor`'s list after each change to
the globs.

**A gate that is a lane group** takes `inputs` in one of two ways, never both:

```json
"lanes": {
  "unit": {"command": ["python3", "scripts/qk", "unittest", "-s", "tests"], "inputs": ["src/python/**", "tests/**"]},
  "app":  {"command": ["xcodebuild", "test", "-scheme", "App"], "inputs": ["apple/**", "*.xcconfig"]},
  "gate": {"parallel": ["unit", "app"], "resource": "heavy"}
}
```

- **On its lanes**, as above: a batch runs the group with only the lanes whose `inputs` a changed
  path matches, under the group's one admission. A lane of the group without `inputs` runs
  whenever the group does, and makes the group run for every change. The round's log says
  `gate: only unit of its lanes`, and the lanes left out show as `NOT SELECTED` in the group's
  output, with `notSelected` in its receipt.
- **On the group itself**: all of its lanes run, or none.
- **A group of which only some lanes ran** does not cover the pre-push hook for
  `queue.hookCoveredBy`, and a bisect's halves, a stacked gate and the pre-push hook run the
  whole group.
- **Carry-over reads the group's lanes.** When `main` moved only by paths of the group's lanes
  that the batch's paths did not choose, the batch's pass still stands.
- **By hand**, `qk run gate -- unit` runs only the lanes named after `--`, as the queue does.

**`queue.hookCoveredBy` skips the pre-push hook only when every lane it names ran**, and whole.
`qk pre-push` selects gate lanes by the same `inputs`.

`queue.affected`, the older way to write the same map from the paths to the lanes, is refused: the
refusal gives the `inputs` that select the same lanes. Under it a path no rule covered ran every gate
lane, where a path no lane's `inputs` name now runs none.

Lane `inputs` also extend carry-over. When `main` moved only by paths that the batch does not
touch, that `inputs` name, and that select none of the gate lanes the batch's own paths selected,
its pass still stands: the lanes it ran do not read what `main` gained, and `main` gated those
paths with their own lanes. The batch is pushed without gating again, after the project's preflight lanes,
`qk docs check` and any `queue.carryOverChecks` pass on the rebased tree. A project with a
`provision` lane carries over only unread paths, since provisioning sets up every gate lane.

### The pre-push hook

After a gate passes, the worker runs the project's pre-push hook itself, as Git would for a push to
`main`, then pushes. An exit of 3, 69, 75 or 78 gives no verdict. The worker retries the hook
once after the host settles. A definitive hook refusal or an exhausted retry sends back the batch
with the hook log and doctor reports. A hook that waits for admission should return the admission
result so the worker can recognize a retryable outcome.

An explicit server rejection of the `main` update also sends back the batch once the worker
accounts for every attempt. A push without a definitive answer stays in publication recovery,
including when a later attempt is rejected. Repository
access problems, such as invalid credentials, keep the passing batch for an operator to resume.

**A hook that only repeats the lanes** can be one line, which runs the right lanes for the paths a
push changes: the docs lanes for documents, `queue.lanes` or `gate` for anything else, narrowed by
the lanes' `inputs` as the queue narrows them, nothing for a push that changes
no path. It reads the checkout's own `queuekeeper.json`, not `main`'s. It exits 0 on a pass, 1 on a
failure and 3 when a lane was unmeasured, which refuses the push. Under the queue's worker it runs nothing, since the lanes just ran.
`QUEUEKEEPER_PREPUSH=skip git push` skips it and says nothing was verified.

```sh
#!/bin/sh
exec python3 scripts/qk pre-push "$@"
```

**A hook of the project's own that the lanes cover** can be skipped by the worker. The project
names, on `main`, each lane that covers it and what of the hook it runs; the lanes must be ones the
queue runs:

```json
"queue": {"lanes": ["gate", "app-gate"],
          "hookCoveredBy": {"gate": "check-all.sh, the hook's first step", "app-gate": "check-app.sh, its second"}}
```

**Keep `core.hooksPath` right.** Git silently runs no hook, or another worktree's, when
`core.hooksPath` is absolute, set per worktree, or names a missing folder. Name the hooks folder in
`queuekeeper.json` and `qk doctor` sets and repairs it:

```json
"gitHooks": "tools/repository/hooks"
```

`qk doctor --hooks` does only that, in about a second, for a session-start hook. It exits 0 when
nothing is left wrong and 1 when a warning remains:

```json
{"type": "command", "command": "python3 scripts/qk doctor --hooks", "timeout": 15}
```

### Changes that build on each other

Queue dependent pieces together, in order, instead of landing and waiting for each in turn:

1. Give each piece its own branch, holding the pieces before it.
2. Run `qk land BRANCH` for each, oldest first, without waiting in between. The branches need
   different names, since landing a branch again replaces its earlier entry.
3. Follow them all with `qk queue wait ID ID ...`, which exits 0 when each has landed.

They usually ride one gate. Each piece lands once, as its own commit. If an earlier piece comes
back, the later pieces that hold its commits come back with it.

### Changes main's copy judges

Some of what judges an entry comes from `main` or the project's main checkout, not from the entry.
A change to one of these pieces is judged by the old copy, so an entry that changes the piece and
starts relying on the change in the same commit is refused, though it is right. Land it in two
steps: the change to the piece alone first, then, once that is on `main`, what relies on it.
Queuing them together as [changes that build on each other](#changes-that-build-on-each-other)
does not help, since the second is still judged by `main` as it was.

- **Preflight lanes.** `qk land` runs the lanes `main`'s `queue.preflight` lists, as `main` has
  them, on the entry. To remove or replace a preflight lane, or what it reads, land the change to
  `queue.preflight` and the lane first. A refusal by a lane the entry drops or changes says so.
- **Plugins loaded from inside the project**, such as `{"module": "NAME", "path": "src/python"}`.
  A lane in a linked worktree, the queue's checkout included, loads them from the main checkout.
  A rejected entry that changes such a folder is told that main's copy judged it.
- **A pre-push hook outside the queue's checkout.** When `core.hooksPath` is absolute and names
  the main checkout's hooks, the worker runs that copy. Keep what it reads working for one landing
  and drop it in a second. A send-back for its refusal names the folder it ran from.

### Emergencies

These are for a fix that cannot wait, never routine, and never for a flaky test.

- `qk land --front` tests an entry next, as a batch of its own, ahead of everything waiting. A
  running gate finishes first. `--why` is optional.
- `qk land --no-gate --why "..."` lands the entry alone, in its turn, with only the preflight lanes
  and the pre-push hook. The reason is required and recorded in a `Landed-without-gate:` trailer,
  and status lists the commit as ungated until a later gate covers it. Tell the owner you used it
  and why.
- With both flags, the entry lands next. It overtakes a round whose gate is still waiting for
  admission; a gate that has started finishes first.

Running the same flags again on an entry already queued updates it.

Push to `main` by hand only when `qk land` cannot be used, such as a broken queue, or with the
owner's approval, and report it with its reason. The queue takes a hand push as it comes.

### Closing a queue for maintenance

Maintenance that needs an empty queue, such as moving its state or taking a Mac away, closes the
queue first. Checking that the queue is idle and then acting on it would leave a gap where a new
entry could arrive.

```sh
qk queue close --why "moving the queue's state" --for 2h && {  # refuse new entries for up to 2 hours
  until qk queue status --idle; do sleep 30; done              # let the worker finish what it has
  # ... the maintenance ...
  qk queue reopen
}
```

- **Idle means nothing left to land.** `qk queue status --idle` exits 0 once no entry is queued,
  testing or passed, so no push is pending. A worker may still run then: after its last landing it
  waits to move the shared checkout, `~/Development/queuekeeper`, to `origin/main` until no process
  runs from it, which can be another project's queue worker for as long as its gates take. That
  waiting does not block maintenance, and `qk queue status` says so under it:

  ```text
  worker: running
    waiting for the shared checkout to follow origin/main once nothing runs from it, for 40m41s
      held by the queue worker for mpm, PID 26815, running 84m49s
      this waiting does not block maintenance: no entry waits or tests and no push is pending, so `qk queue status --idle` exits 0
  ```

  Maintenance that itself moves, replaces or edits the shared checkout is the exception: wait
  until `qk queue status` no longer shows that waiting, or the worker's next try may meet the
  maintenance half done. Maintenance on the queue's state or a Mac does not
  touch the checkout and need not wait.

- **A closure has a deadline.** It lasts an hour unless `--for DURATION` says otherwise, for
  example `--for 30m` or `--for 2h`. The longest allowed is 24 hours. When the deadline passes,
  the queue reopens by itself, so a session that crashes or forgets to reopen cannot block every
  landing. The journal records this as `queue.lapsed`.
- **Renew a closure for longer maintenance.** `qk queue close --extend [--for DURATION]` moves
  the deadline to DURATION from now (an hour by default), like a lease renewal. A long
  maintenance script runs it inside its wait loop. `--extend` exits 1 when the queue is not
  closed, including once the closure has lapsed. A second `close` without `--extend` exits 1 and
  leaves the first closure, and its deadline, unchanged.
- **A closed queue refuses new entries** with exit 1:
  `COMMIT is not queued: the queue is closed to new entries by WHO since WHEN (AGE ago): WHY`.
  The refusal then says when the queue reopens by itself. The JSON `recovery` gives the
  deadline as `reopensAt` and suggests `qk queue closed --json`.
- **`qk queue closed` says whether the queue is closed:** by whom, why, how long it has been
  closed, and when it reopens by itself. It exits 0 while the queue is closed and 1 while it is
  open, so `while qk queue closed; do sleep 30; done` waits for the reopening. With `--json` it
  gives `closed`, `closure`, `closedSeconds` and `reopensInSeconds`.
- **Entries already queued still reach their verdict.** Closing is not a hold: `qk queue resume`
  leaves a closed queue closed, and `qk queue reopen` clears no hold.
- **`qk queue status` shows the closure** on its second line, with its deadline. In `--json` it
  is `closed`, which is null while the queue is open. `qk queue doctor` reports a closure as a
  finding and says if the worktree it was closed from is gone. The dashboard shows it under
  attention.
- **Whoever closed the queue reopens it** when the maintenance is done. If that session is gone,
  any session may run `qk queue reopen`. A session whose entry was refused waits for the queue to
  reopen, then lands again.

### Entry names

Entry IDs look like `qk7318-issue-123-fix-share-sheet`: `qk`, a random four-digit number, an
`issue-N-` prefix when the branch or commits reference an issue in this repository, and a slug of
the branch name (without an `agent/` or `codex/` prefix) or of the commit subject. A commit's closing
keyword, such as `Closes #123`, takes priority over other mentions.

### A project's first landing

The commit that adds a project's `queuekeeper.json` lands through the queue like any other, with
`qk land --wait`. The queue normally reads settings from `origin/main`, so that no branch chooses
its own gate; until `main` has a `queuekeeper.json` naming lanes, it reads the entry's for that
landing alone, and says so. After that commit lands, every later entry is gated by `main`'s
settings.

Settings that loosen what the queue checks count only from `main`, even on the first landing:
`queue.hookCoveredBy`, `queue.unread` and `queue.preflight`. A project that wants one of them lands its
configuration through the full checks once.

A repository created empty has no `main` at all. Its first `qk land` creates it: the worker gates
the entry's commit alone, on its own `queuekeeper.json` as above, runs the pre-push hook, and
pushes that commit as `main` only while `origin` still has none, so nothing ungated lands and
nothing is rewritten. Until then:

- a commit without a `queuekeeper.json` naming a gate lane is refused, exit 1;
- only one entry waits to create `main`; another is refused naming it, and `qk land --wait`
  waits for it, then lands on the `main` it made;
- `--no-gate` is refused;
- a `main` someone pushes while the gate runs sends the entry back into the queue, which lands it
  on top of that `main`.

### When main's configuration can't be read

A Queuekeeper release can refuse a setting that it only warned about before. A project whose
`main` still has that setting can't have its `queuekeeper.json` read from `origin/main`. Then
`qk land` says that the fault is in `origin/main`'s file, not the entry's, and names the setting.

- **An entry that fixes it lands through the queue.** When the entry's own `queuekeeper.json`
  reads, the queue gates that landing, and the round's batch, by the entry's file, as for a
  project's first landing. It says so. The settings that loosen what the queue checks still count
  only from `main`, so they are left out. `main`'s file is used again from the next landing on.
- **Any other entry is refused**, since its own file is `main`'s, unchanged. Land the fix first.
- **A branch that is behind** is told to rebase instead, when `main`'s file changed after the
  branch left `main` and the branch's own copy from that point still reads. A branch's
  Queuekeeper can predate a key that `main` added since, and rebasing brings in the code that
  knows it.

## Lanes and admission

### Lanes

A lane is a named command in `queuekeeper.json`, with a timeout and a statement of what it needs
from the Mac. `qk run LANE` runs one, for an agent or for the queue.

```json queuekeeper.json
{
  "schemaVersion": 1,
  "name": "my-app",
  "plugins": [],
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
| `timeout` | Seconds before the lane ends as unmeasured; 2 hours when unset ([Time limits](#time-limits-and-the-gates-budget)) |
| `parallel` | Makes the lane a *group* that runs its members side by side under one admission ([Lane groups](#lane-groups)) |
| `jobs` | How many of a group's members run at once; all of them by default |
| `failFast` | Ends a group at its first failed member ([Lane groups](#lane-groups)) |
| `resource` | `"exclusive"` for timing measurements that need the Mac to themselves; `"self"` for a script that admits its own parts |
| `wholeHost` | The lane starts its own parallel jobs and needs the whole Mac |
| `here` | Why the lane runs only on this Mac, such as a physical device |
| `unlockedScreen` | Runs only on a Mac whose screen is unlocked ([Screen lock](#screen-lock)) |
| `labels` | Runs only on a Mac whose settings give it every label, such as `["unity-license"]` |
| `compileFirst` | A build command run before the tests, in the same admission ([Compiling before the tests](#compiling-before-the-tests)) |
| `minimumTests` | Rarely needed: a typed floor beside the [count from history](#test-counts), which already fails a whole run that executes fewer tests than the lane's last pass on main |
| `exactTests` | With `minimumTests`, a whole run must execute exactly that many, for a lane that picks its tests by name |
| `arguments` | `false` refuses `qk run LANE -- ARGUMENTS` for a lane that selects its own tests |
| `budgetMinutes` | Warns when the gate's median time drifts past this target, naming the slowest parts |
| `inputs` | For a gate lane, the globs of the paths that affect it; a change to none of them skips it ([Gate lanes chosen by changed paths](#gate-lanes-chosen-by-changed-paths)) |
| `failedTests` | Lets the queue rerun a non-Python lane's failed tests ([When a batch fails](#when-a-batch-fails)) |
| `stall` | Reports, and optionally stops, a lane that has gone quiet ([Time limits](#time-limits-and-the-gates-budget)) |
| `timing` | Warns while a lane runs past its usual time, and optionally fails it past a threshold ([Time limits](#time-limits-and-the-gates-budget)) |
| `notice` | What an agent is shown when the lane's timeout, stall bound or timing threshold stops it |
| `disk` | `{"startFreeGB": N}`, the lane's own free-space floor before admission ([Free disk space](#free-disk-space)) |

[`schemas/queuekeeper.schema.json`](../schemas/queuekeeper.schema.json) lists every setting.

- **Arguments.** `qk run LANE -- ARGUMENTS` appends arguments to the lane's command, to run some
  of its tests. A lane with `"arguments": false` refuses them at once, with exit 2.
- **Results.** Each lane's command finds an empty folder of its own in `QUEUEKEEPER_RESULTS_DIR`
  for transcripts, result files and failure records. Read the variable rather than naming a path,
  since a group's members each get their own. Whatever is written there comes back from another
  Mac however the run ends, and a whole lane's results are kept by commit beside its receipt.
- **Where the project is.** A lane's command finds the project's main checkout in
  `QUEUEKEEPER_PROJECT_CHECKOUT` wherever it runs.

### Receipts and outcomes

Each run of `qk run LANE` writes its log, `.build/queuekeeper/LANE/run.log`, and a receipt,
`source.json`, beside it: the commit and a hash of any uncommitted changes, the command and
arguments, the exit status and outcome, the Mac, the wait for admission, the test counts and the
Mac's load while it ran. The load is read from admission to the run's end, never during the wait,
and the summary's `load:` line gives its highest and last one-minute load, the number of readings
and the highest CPU busy share, passed or not. A lane or lane group sent with `qk remote run` keeps
that Mac's receipt under `result.receipt` and ends with a `remote: load on MAC` line saying the
same. Whole-lane runs on a clean commit are also kept by commit in the
repository's Git directory, so any worktree can ask whether that commit already passed a lane.
`qk admit -- COMMAND` writes the same receipt, without a log, to `.build/queuekeeper/admit/`, and
the next admitted command replaces it: copy it when it is evidence.

| Outcome | Exit | When |
|---|---|---|
| `passed` | 0 | The command exited 0 and ran enough tests |
| `failed` | the command's; 2 for too few tests | The command failed, or exited 0 with too few tests |
| `failed`, before tests | 4 | The log shows no test started, such as a project that could not be generated; the summary quotes the last error |
| `build-failed` | nonzero | The lane's `compileFirst` step failed; no test ran |
| `unmeasured` | 3 | The command exited 3, a test reported itself unmeasured, the `timeout` stopped it, or the checkout moved to another commit during a run on this Mac ([why](#how-the-code-gets-there)) |
| `not-started` | 127 | The command could not start, such as `python3` given a file the checkout lacks |

A command that cannot measure something, such as a timing check voided by load, exits 3 and says
why in a log line beginning `Unmeasured:`, which the queue quotes in a hold. A run that failed
without naming a test ends its summary with the last lines its command printed.

### Test counts

Queuekeeper reads executed and skipped test counts from XCTest, Swift Testing, unittest, pytest
and `qk unittest` output in the lane's own log. Skipped tests ran nothing and never count toward a
floor. With no number in `queuekeeper.json`:

- **A run that reports a count must run at least one test.** One whose every test skipped fails.
  A lane whose log reports no count, such as a spelling check, is held to nothing.
- **A whole run must run as many tests as the lane's last passing whole run on `main` on the same
  Mac.** A Mac with no such run is held to the newest on any Mac, by the tests it selected (executed
  plus skipped), so tests that skip on only one of the two Macs do not count against it. A run that
  falls short fails with exit 2, naming the count, the Mac, the commit and the receipt to compare
  with. A count that rises needs nothing. When no eligible receipt exists on any Mac, the run has
  no history floor. Runs with arguments skip the history check.
- **A whole lane group checks each member's count independently.** Each member reads its counts
  from passing whole runs of that group on `main`. The same checks apply with `--serial` and
  when an outer gate invokes the group. The group's whole, passing parallel runs supply baselines.
  A run with member-selection arguments or `--serial` leaves those baselines in place.
- **Removing tests on purpose** takes a trailer in any commit of the change, naming each lane whose
  count falls, or `*` for every lane:

  ```text
  Tests-Removed: gate, unit
  ```

  For a group, name the member whose count fell. If an outer gate also checks its own aggregate
  count and that count fell, name the outer gate too.

  In the merge queue, a gate that falls short sends back the entry that removes tests without
  this trailer, and the rest of the batch gates again without it.

`minimumTests` is a typed floor for a lane that must not fall below a number even on a Mac with no
pass yet; most lanes need none, since a typed floor drifts. A lane that picks its tests by name sets
`"exactTests": true` beside it, so a whole run must execute exactly that many, and the error names
the count to set. `qk run --expect N LANE` requires exactly N of one run ([Verifying](#verifying)).

A gate that writes each step's output to a separate log leaves no count in its own; print the
steps' output to the lane's output too, or the lane is held to no count.

### Compiling before the tests

A lane whose tests need a build first names that build in `compileFirst`:

```json
"mac-tests": {
  "command": ["scripts/app.sh", "mac-test-unsigned"],
  "compileFirst": ["xcodebuild", "build-for-testing", "-project", "app/App.xcodeproj", "-scheme", "App",
                   "-destination", "platform=macOS", "-derivedDataPath", ".build/native-tests",
                   "CODE_SIGNING_ALLOWED=NO"],
  "resource": "heavy", "timeout": 3600
}
```

It runs on the lane's Mac, in its one admission and within its `timeout`, before the command;
arguments after `--` go to the command alone. A failed build ends the run as `build-failed`, and a
passing build's output goes to `compile.log` so `run.log` holds only the tests. A `self` lane
cannot have the step, and a group runs its members without theirs. `qk xctest` and
`qk simulator-tests` compile first already. For `qk admit`, chain the two in one admitted job:
`qk admit -- bash -c 'BUILD && TESTS'`.

### Sandboxing a lane

A lane can run its command under macOS's Seatbelt sandbox (`sandbox-exec`), so tests an agent
wrote cannot write outside the checkout or reach the network freely:

```json
"unit": {"command": ["python3", "-m", "pytest"], "sandbox": true},
"integration": {"command": ["scripts/integration.sh"],
                "sandbox": {"write": ["~/.gradle"], "network": ["localhost", "*:443"]}}
```

The command and its `compileFirst` may write only to:

- the checkout the lane runs in, and its git directory (a linked worktree keeps that outside the checkout)
- the run's directory under `.build/queuekeeper/`
- `/private/tmp`, `/private/var/tmp`, `$TMPDIR`, the user's temporary and cache folders, `~/Library/Caches`
  and `~/.cache`
- the null and terminal devices
- the folders in `write`: absolute, `~/...`, or relative to the checkout

Reading is not limited. The network is off unless `network` says otherwise. Unix sockets inside the
lane's own writable folders still work, so a test's server and client can talk. `network` takes
`localhost`, `localhost:PORT`, `HOST:PORT`, `*.DOMAIN:PORT`, `*:PORT` (any host on that port, with name
lookups), `*` (no limit), or a unix socket's absolute path.

Seatbelt can tell only localhost from other hosts, so a lane that names hosts, such as
`"network": ["github.com:443", "*.githubusercontent.com:443"]`, reaches them through a filtering proxy.
`qk run` runs the proxy on localhost, in its own process and outside the sandbox, for the length of the
run. The proxy admits a `CONNECT` (HTTPS) or plain HTTP request only to a host and port the lane names.
`*.DOMAIN` means any name under the domain, not the domain itself. The profile lets the command reach
only the proxy's port, so a host the lane does not name cannot be reached, by name or by address. The
command is given the proxy in `HTTPS_PROXY`, `HTTP_PROXY` and `ALL_PROXY`, in both cases, with
`NO_PROXY` set to localhost. curl, git, Python's urllib and requests, pip and npm read these
variables. A client that ignores them, such as Node's built-in `fetch`, Gradle without its
`-Dhttps.proxyHost` settings, or SwiftPM, reaches nothing. A lane cannot name hosts and also `*` or
`*:PORT`, which would let a connection past the proxy. The receipt's `sandbox.proxy` gives the proxy's
address. A connection the proxy refused is listed with the run's refusals, as `proxy-connect HOST:PORT`.

The profile is made when the lane starts on the Mac that runs it, after admission has placed it, so a
lane sent to another Mac gets that Mac's paths. It is kept as `sandbox.sb` in the run's directory
(`sandbox-NAME.sb` for each member of a group), and the receipt's `sandbox` lists what it allows.
Before the command starts, Queuekeeper checks that the profile applies on that Mac. If it does not,
the lane ends `not-started`, not failed. A refused write fails the command's own call with
`Operation not permitted`, which says neither path nor address. So while a sandboxed lane runs,
Queuekeeper reads the kernel's refusals of it from macOS's log, and the summary of a run that did not
pass lists them, for example:

```text
  sandbox refused: file-write-create /Users/me/.npmrc (node)
  sandbox refused: network-outbound remote:*:80 (curl)
```

Add the path to `write` or the endpoint to `network` and run it again. The receipt's `sandbox.refused`
holds up to 50. The kernel does not report every refusal: a write a process makes as it starts can go
unreported. A failed run whose log says `Operation not permitted`, with no refusal reported, says that
one may have been refused. `qk doctor` warns of each Mac, this one and the farm's, where `sandbox-exec`
is missing or applies no profile, when a lane has a sandbox.

Admission and [process supervision](#process-supervision) work as for any lane. `qk run` holds the
admission outside the sandbox, and `sandbox-exec` replaces itself with the command, so its time limit
and stop reach the same processes. A group's members each have their own `sandbox` (a group cannot
have one).

macOS cannot apply a sandbox inside another. A lane run inside a sandboxed lane runs under the outer
sandbox, and its receipt says `{"inside": "OUTER"}`. For the same reason:

- **A `self` lane** cannot have a sandbox: its script admits its parts, which may run on other Macs.
- **Xcode and SwiftPM** apply sandboxes of their own (xcodebuild to script phases, SwiftPM to manifests
  and plugins), and they write to `~/Library/Developer`. With the Apple plugin, a lane with `idioms`, or
  whose command or `compileFirst` runs `xcodebuild`, `xcrun`, `swift`, `simctl`, `devicectl`,
  `simulator-tests` or `xctest`, is refused a sandbox when the configuration is read. Run these lanes
  unsandboxed. A script that runs one of them inside a sandboxed lane fails when it starts it
  (`sandbox_apply: Operation not permitted`), and the run's summary says which tool applied its own
  sandbox.
- **Device lanes** reach phones over the network and usbmuxd's or adb's sockets, and write device
  reservations outside the checkout. With the mobile plugin, a lane with `devices` or a `device-pass`
  command is refused a sandbox. With the Android plugin, so is one whose command or `compileFirst` runs
  `adb`, `emulator`, `avdmanager`, `emulator-tests` or `emulator-pool`. Run them unsandboxed.
- **Gradle** is not refused. It runs given `"write": ["~/.gradle"]` and `"network": ["localhost"]` for
  its daemon, plus the ports it downloads from unless it runs `--offline`. Pass `--no-daemon`, so that
  no daemon started under the lane's sandbox serves another build.

### Lane groups

A lane whose `parallel` names other lanes runs them at once under one admission:

```json
"lanes": {
  "mac-tests": {"command": ["swift", "test"], "resource": "heavy"},
  "ios-tests": {"command": ["qk", "simulator-tests", "--scheme", "Package-Tests",
                            "--runtime", "iOS-27-0", "--device", "iPhone-17-Pro", "--"], "resource": "heavy"},
  "tests": {"parallel": ["mac-tests", "ios-tests"], "resource": "heavy"}
}
```

- Each member is judged as `qk run MEMBER` would judge it, and writes its log and receipt to
  `.build/queuekeeper/GROUP/lanes/`. A failed member's last 40 lines print under its result; the
  others still run to their end, unless the group says `failFast`.
- **`"failFast": true`** ends the group at its first failed member. The members still running are
  stopped as their timeout would stop them, with SIGTERM and then SIGKILL, and the rest never start.
  Each is reported `CUT SHORT`, with the outcome `cut short` and no status in its receipt; the group
  fails, and its receipt names the failed member in `stoppedAt` and the others in `cutShort`. A gate
  cut short this way never lands as flaky ([When a batch fails](#when-a-batch-fails)). Leave it off
  for a group whose members need more than SIGTERM to stop cleanly, such as one holding a device.
- A member that fails fails the group. A member that did not run, or ended unmeasured, leaves the
  group without a verdict rather than failed.
- **`qk run GROUP -- MEMBER...`** runs only the members named, under the group's admission. The
  others are reported `NOT SELECTED`, with the outcome `not selected` in their receipts and their
  names in the group's `notSelected`; they count for nothing in the group's status. The merge
  queue runs a gate group this way for the members whose `inputs` a batch changes
  ([Gate lanes chosen by changed paths](#gate-lanes-chosen-by-changed-paths)). Such a run is not a
  run of the whole lane: it is kept as no commit's pass and adds nothing to the timing history.
- The group needs a `resource` at least as heavy as its heaviest member. A lane that takes the
  screen cannot join a group, and a group cannot name another group.
- A group takes no arguments: filter one member instead (`qk run ios-tests -- -only-testing:T/C`).
- A group with a `"model": true` member needs `"model": true` itself; with two such members it
  also needs `"jobs": 1`.
- A heavy group goes to another Mac whole unless it or a member says `here`. A gate script that runs
  a group as one of its steps uses `qk run --here GROUP`, so the results stay with the gate.
- `qk run --serial GROUP` runs the members one after another on this Mac, each to its end even
  under `failFast`, as a reference for a failure seen only side by side; `qk remote run --serial GROUP` does so on another Mac. Neither
  counts as a pass of the group.

Members that share a build folder cannot run side by side: two `swift test` runs in one `.build`
wait on its lock, and two `xcodebuild` runs need separate `-derivedDataPath`s.

### Running tests side by side

| Runner | Side by side | One at a time |
| --- | --- | --- |
| `qk unittest` | `qk unittest -s tests -j N`: a process per module, four by default | `-j 1` |
| XCTest in a Swift package | `qk xctest -j N`: classes packed into xctest processes | `swift test` |
| Simulators | `qk simulator-tests --device T --device T`: devices at once | `--jobs 1` |
| xcodebuild | `-parallel-testing-enabled YES -parallel-testing-worker-count N` | `-parallel-testing-enabled NO` |
| Swift Testing | Concurrent in one process by default | `swift test --no-parallel`, or `.serialized` |
| pytest | pytest-xdist: `-n N --dist loadgroup` | `-n 0` |

Keep tests that share state out of each other's way, with a serial group or a serialized suite for
the few that must share, rather than turning parallelism off. Don't use `swift test --parallel`:
it reports skipped XCTest tests as passed and prints no total to count.

#### `qk unittest`

`qk unittest` discovers a suite as unittest does and runs each module in its own process.

- **Selecting.** Name modules, classes or tests after the options
  (`qk unittest -s tests integration.test_merge_queue.Landing`), or filter with `-k PATTERN`, an
  fnmatch pattern or substring on the test ID. `-p PATTERN` limits discovery by file name, and
  repeats. A name or pattern that selects nothing exits 5 and runs nothing.
- **Splitting.** `--split class` or `--split test` runs each class or test in a process of its own;
  `--batch-size N` caps the tests per class process. A module that sets `shard_by = "class"` (or
  `"test"`) splits that finely anyway, and one that sets `serial_group = "NAME"` runs in one
  process with the rest of its group, in turn. The slowest process bounds the run, so split a
  module that dominates.
- **Counting.** Each process's output prints whole under a line naming it. A process that ran fewer
  tests than discovery found for it fails.
- **Progress.** The first line names a folder under `.build/queuekeeper/runs/` that `qk progress`
  reads ([Long runs](#long-runs)).
- **Unfinished runs.** A process past `--timeout` is unmeasured. A stopped run still prints its
  summary, lists the unfinished tests, and gives the command that runs only them, also saved in
  `unittest/unreached.txt` in the lane's results.
- **Failing fast.** `--fail-fast` reruns a failed process's failed tests alone at once and, if one
  fails again, stops the run and prints the tests left out with the command that runs only them. A
  failure that passes alone lets the run go on. Such a run starts with the test classes that failed
  in the repository's `--fail-fast` runs of the last 14 days, the latest first, since a failure
  tends to come back where one just was. Queuekeeper's own gate runs this way.
- **Admission.** Started outside a lane, `qk unittest` is admitted like `qk admit`. `--unadmitted`
  is only for testing a runner itself.

#### `qk xctest`

`qk xctest` builds a Swift package's tests once, lists them, and packs the XCTest classes into
`-j` processes, longest first by the last run's times. Each process runs one test at a time and
reports its skips; the Swift Testing tests run once beside them.

- `-C PACKAGE`, repeated, tests several packages, which share the processes.
- `-c release`, `--scratch-path PATH` and `--swift-option OPTION` go to every swift command.
- `--filter REGEX`, repeated, selects by `Module.Class/test`, as `swift test --filter` does.
- `--split-swift-testing` puts Swift Testing suites into the shards too, one test at a time, for
  suites that interfere when run together (such as main-actor suites sharing a window or fonts).
- `--skip-build` reuses the last build.

A process that fails, or runs other than the tests it was given, fails the run. A skip whose reason
begins `Unmeasured:` leaves a passing run unmeasured (exit 3). A filter that selects nothing exits 5.

Swift Testing prints a case of a parameterized test that `Test.cancel` canceled as passed, and its
test too; only its event stream records the cancellation. `qk xctest` reads each Swift Testing
process's event stream and prints, after its output, a line for each test with a canceled case,
`➜ Test NAME was cancelled after S seconds in K of its N test cases: "REASON"`, with a line for each
case under it. The test counts once as skipped, however many of its cases were canceled, and as
unmeasured when a case's reason begins `Unmeasured:`. Use `qk xctest` for Swift package lanes
that rely on parameterized cancellations. A lane that runs `swift test` directly only has its
console output for counting, which reports those cases as passed.

#### `qk simulator-tests`

`qk simulator-tests` builds a scheme's tests once and runs them on every named device at once:

```sh
qk simulator-tests --scheme SCHEME --runtime iOS-27-0 \
  --device iPhone-17-Pro --device iPad-Air-13-inch-M4 --build-jobs 4 -- -only-testing:TARGET/CLASS
```

- Each device is a member of the project's simulator pool, reserved, reset from its kind's warmed
  template (`--erase` erases it; `--keep-state` keeps it as it was), and released however the run
  ends.
- Devices boot while the build runs. Each then runs the same build with its own log, result bundle
  and derived data.
- Arguments after `--` go to every device's test run. A lane that runs `qk simulator-tests` ends
  its command with `--`, so `qk run LANE -- -only-testing:T/C` lands there.
- `-C DIRECTORY`, `--xcode-project` or `--workspace` say where the scheme is; `--setting NAME=VALUE`
  adds a build setting. `--build-only` builds and tests nothing, to warm a build a later run reuses.
- Counts come from each device's result bundle. A passing Swift Testing test with canceled
  parameterized cases counts once as skipped, or as unmeasured if a case's reason begins
  `Unmeasured:`. A device that ran no test or reported only ordinary skips fails. Read or copy
  the bundles before the next run with the same `--logs` folder.
- One run at a time uses a `--derived-data` and `--logs` folder; another waits and prints `WAIT:`.
- `--jobs N` tests N devices at a time. `--workers N` turns on xcodebuild's parallel testing on
  each device; leave it off until a suite has been shown to pass with it. `--shards N` instead
  splits each device's tests across N pool members, packed by the last run's times.
- `--boot-timeout` (600 s) leaves a device that will not boot unmeasured. `--test-timeout` (2 h)
  and `--test-allowance` bound the run and each test ([Time limits](#time-limits-and-the-gates-budget)).
- Two simulators at once can hold memory pressure at warning for the whole run, which stops
  admission from opening extra slots, so keep memory-ceiling measurements out of such a run.

Each run keeps per-test outcomes, seconds and issue messages in `simulator-tests/` of the lane's
results. The per-device or per-shard JSON records messages in each test's `issues` list, including
any source location the result bundle gives. The kept log also includes Swift Testing's issue
comments when Xcode prints them. For known issues the bundle can give just the
`withKnownIssue` scope's message.

To collect measurements from a simulator test, give it a local output directory through an
Xcode test-runner variable:

```sh
measure_dir="$(mktemp -d)"
TEST_RUNNER_MEASURE_DIR="$measure_dir" qk run --here ios-tests
```

In the test, read `MEASURE_DIR` from `ProcessInfo.processInfo.environment` and write the measurements
there. Xcode strips `TEST_RUNNER_` before passing the name to the test process. Use a separate
filename for each test and simulator if they write concurrently. A lane configured with
`qk simulator-tests --forward-env PREFIX` also passes variables that start with `PREFIX` to
its tests, with their original names.

A script that runs `xcodebuild test` itself keeps the same record by calling, once per run, pass or fail:

```sh
qk results keep --log "$log" --bundle "$bundle" --name "$device" --status "$status"
```

It prints each test's outcome and seconds, exits 3 when the bundle cannot be read, and exits 1 when
NAME is already in this run's results. `--activity-timing-prefix PREFIX` also keeps XCTest
activities titled `PREFIX N s`. Keep a system trace in the results when a test reports a long wait,
since the next run on that Mac replaces the logs.

Use `qk results history LANE TEST` to see one exact test ID across that lane's retained Xcode
results. Use the ID printed by `qk results keep` or in the kept JSON. Each observation names its
commit and result file, so results from different shards or simulators can be distinguished.
These observations come from the latest whole run kept at each commit and reflect the results
still on disk. The merge queue records proven flakes separately.

#### A pytest lane

Run pytest with `-rs --no-fold-skipped`, so each skip's reason, including an `Unmeasured:` one, is
printed per test:

```json
"python-tests": {"command": [".venv/bin/python", "-m", "pytest", "-n", "4", "--dist", "loadgroup",
                             "-p", "no:cacheprovider", "-rs", "--no-fold-skipped"],
                 "resource": "heavy"}
```

The count is pytest's last summary line: passed, failed, skipped, xfailed and xpassed tests ran;
deselected tests and errors did not. A plugin that adds a word to that line, such as
pytest-rerunfailures' `rerun`, leaves a line Queuekeeper cannot read.

### Repeating a lane's tests

One passing run does not show that a test that failed under load is fixed. Run it as a series:

```sh
qk run tests --repeat 10 --erase --only BreezEPUBTests/QuickLookTests
```

`--repeat N` runs the lane's `qk simulator-tests` tests N times on one build, each on a pool member
taken afresh. `--erase` erases the member before each run, and `--template` copies its kind's warmed
template instead. `--only TEST`, repeated, replaces the lane's own selection; a wrapper script must
pass its arguments on for this to work. The series holds one admission with N times the lane's
`timeout`. Each run prints its outcome, member, counts, seconds and load; `series.json` in the
lane's results keeps them all. One failed run fails the series. A series is not a pass of the lane.

### Time limits and the gate's budget

Every run has two limits, so a hang ends instead of holding a slot:

- **Each test** has one, so a hung test fails by itself. `qk simulator-tests` gives XCTest a
  per-test allowance (`--test-allowance`, 600 s). A script that runs `xcodebuild test` itself
  passes `-test-timeouts-enabled YES` and an allowance too, and a Swift Testing suite gives its
  tests a `.timeLimit`.
- **Each command** has one: a lane's `timeout`, `qk admit --time-limit`,
  `qk simulator-tests --test-timeout`, or `--timeout` for each process of `qk unittest` and
  `qk xctest`. A lane or `qk admit` command that sets none stops after 2 hours, and a
  `qk unittest` or `qk xctest` process after 30 minutes. At its limit the command and its whole
  process group are stopped, and the run is unmeasured, never passed.

`qk doctor` lists the lanes that rely on the default. Give each a `timeout` of about twice its
longest measured run.

**Quiet lanes.** A lane can report when it has made no progress (no log output, no CPU, no new
process) for a while, with `"stall": {"seconds": 600}` on the admitted lane that does the work,
never on a group. The current and longest quiet interval go to `results/progress.json`, and
`qk queue status` and `qk admissions now` show them. Once healthy runs have been measured, the lane
can stop on a stall:

```json
"stall": {"seconds": 600, "action": "stop", "reason": "Healthy runs were quiet at most 240 s"}
```

A stopped lane is unmeasured (exit 3). A hook or script that turns every nonzero exit into 1 must
keep 3 first. `"primaryControl": true` leaves out the CPU of a polling controller, such as
pytest-xdist's main process, so its polling cannot hide stuck workers.

**The gate's budget.** `budgetMinutes` on `gate` is a target for its own work, measured from
admission to the end, not the wait for admission. When the median of the last 10 passing runs on an
unloaded Mac (at least 3) passes the budget, `qk land` and `qk doctor` warn after each landing,
naming the slowest parts and, beside them, the median wait. Runs on a Mac past its busy limit are
left out, except whole-host gates. When the warning appears, look for an open budget proposal on
the project's issue tracker. If none exists, open one naming which check could move to
`release.lanes`. Add a reading to an existing proposal only if it adds materially different
evidence, such as a much higher median or fewer runs left out for load. The owner approves a move
or a higher budget.

`qk doctor` also warns of a `gate` with no budget, and of a `gate` that takes a physical device.

**Running past its usual time.** Each passing whole run of a lane adds its own time, from its
command's start to its end without the wait for admission, to the Mac's timing history. A lane's
baseline is the 90th percentile (p90) of its last 20 passing runs on that Mac, once there are at
least 5. While a lane runs, it warns once when it passes its baseline:

```text
queuekeeper timing: warning: gate has run 14m10s, past its p90 of 12m05s over its last 20 passing runs on this Mac (median 11m30s)
```

The warning goes to the lane's log and to `qk run`'s output, which the queue's gate log keeps.
`qk queue status` and `qk land --wait` (or `qk queue wait`) show it beside the running gate as
`timing: warning: ...`, including for a gate on another Mac. Their JSON results include it under
the gate's `timing`, and `qk queue result --json` lists it with any notice in `timing`. A warning
only informs; the lane goes on.

A lane can also fail when it runs too far past its baseline:

```json
"gate": {"command": ["..."], "timeout": 3600, "timing": {"fail": "+25%", "minSamples": 10}}
```

`fail` is relative to the p90: a percentage (`+25%`) or a time (`+90s`, `+2m`, `+1h`). It applies
only once the baseline has `minSamples` runs (10 by default, at least 5). Past it, the lane is
stopped as a stall is, and fails (exit 1) rather than going unmeasured: a lane that became that
much slower is the change's to explain. A busy Mac can push a run past it too, so leave room.
`"warn": false` turns the warning off and keeps a `fail` threshold.

A lane group has no `timing` of its own. Each lane in it is timed against its own baseline as it
runs in the group, with its own `timing`, and a passing lane adds to its own history. A lane past
its threshold fails the group. The group's live record holds each lane's warning.

**What a stopped lane tells an agent.** When a lane's `timeout`, stall bound or timing threshold
stops it, it prints a notice to its log and its output:

```text
queuekeeper notice: gate reached its timeout: do not raise the limit just to make the gate pass: ...
```

By default the notice says not to raise the limit just to make the gate pass, but to find what made
the lane slower. A lane's `notice` replaces it, and a stall's own `"notice"` replaces it for
a stall. A lane in a group that its own limit stops shows its own notice. Raising a limit is the owner's call.

**After a deliberate change to a lane's work.** When a change makes a lane do more on purpose, mark
its timing history stale so that earlier, shorter runs stop setting off warnings, thresholds and the
gate's budget:

```sh
python3 scripts/qk timing stale gate --reason "UI tests moved into the gate"
```

Use `--all` in place of lane names to mark every lane. The baseline then waits for 5 new passing
runs, the fail threshold for `minSamples`, and the gate's budget median leaves out gate runs that
settled before the mark. A run sent to another Mac carries the mark there. The mark is kept on the
Mac where you ran the command, which is also where the queue's worker runs. `qk timing show` (or
`--json`) lists each lane's baseline, fail threshold and latest stale mark. The [dashboard](#the-dashboard)'s Trends tab
draws them over each lane's recent passing runs.

### Waits in script tests

Use `queuekeeper.waiting.wait_until` with a progress probe for work whose duration varies, such
as a build or a simulator run. Set the stall bound for the longest expected gap between signs of
progress. For a command that normally finishes in seconds and only needs protection from a hang,
use a timeout such as `timeout=waiting.stretched(30)`. Process probes that call `ps` can add
substantial overhead on a busy Mac.

In [BreezEPUB's purchase preparation test](https://github.com/vocaro/BreezEPUB/issues/67), the
fake lane normally took about 2 seconds. The same admitted runs alternated a `ps`-based
progress wait with `subprocess.run(..., timeout=30)`.

| Mac and one-minute load | Progress wait | Subprocess timeout |
|---|---:|---:|
| Workstation, about 40 | 7.7 s | 9.9 s |
| Workstation, rising to about 195 | 98.4 s | 16.2 s |
| Build host, about 400 falling to 275 | 93 s | 12.9 s |

A separate `ps -A` listing took 5 seconds on the workstation at a load of 180, so repeated
process listings may have contributed to the slowdown. The 93 s and 98.4 s measurements cover
whole runs. The test switched to a stretched subprocess timeout.
See [#92](https://github.com/vocaro/queuekeeper/issues/92) for the measurements.

`qk doctor` flags fixed waits in tracked Python tests and their fixture source, including a
literal `timeout` default of 20 seconds or more passed directly to a recognized wait.
Use `# wall-clock: REASON` on a bound that measures the behavior under test or specifies a
cleanup grace period.

### Every test has a lane

`qk doctor` reports `testLanes`: whether every XCTest class, Swift Testing suite and Python test
module in the tracked source is selected by some lane. It reads `swift test`, `qk xctest`,
`qk unittest`, `python3 -m unittest discover`, `xcodebuild test` and `qk simulator-tests` commands
and their filters. Its result is `every-test-has-a-lane`, `tests-without-a-lane` (listed under
`noLane`), `could-not-tell` (some lane runs a script it cannot read) or `no-tests-found`. Only the
first is a pass, and none changes a gate.

For a lane it cannot read, say what it selects; and name test paths kept only as records:

```json
"testLanes": {
  "selects": {"ui": ["UITests/"], "spelling": []},
  "skip": {"Tests/Archive/": "old fixtures kept as records"}
}
```

With the Apple plugin, `qk doctor` also warns of a lane that runs one selection on both an iPhone
and an iPad simulator, since tests that do not depend on the idiom then run twice. A lane in
`release.lanes`, a lane per idiom, or a lane whose `idioms` says why its whole selection depends on
the idiom gives no warning.

It warns too of lanes that run one selection once per appearance or content size, where only a
view's snapshot tests should multiply: a lane whose command names two values of either, or two lanes
whose commands differ only in them. It reads `--appearance`, `--content-size`,
`--content-size-category`, `--dynamic-type`, `-AppleInterfaceStyle`,
`-UIPreferredContentSizeCategoryName` and `xcrun simctl ui DEVICE appearance|content_size VALUE`,
also inside a `bash -c` line. Run the other tests once, or say in the lane's `dimensions` why its
whole selection depends on them, such as that it selects only snapshot tests. A lane in
`release.lanes`, or lanes whose selections differ, such as a deliberately narrow second appearance,
give no warning. Like the idiom check, it reads only the lane's own words: a script that sets the
simulator's appearance itself is not seen.

And it warns of a UI test target whose direct `waitForExistence` or `waitForNonExistence` calls are
spread over more than one file. XCTest first checks such a wait a second after it starts, so each
wait for an element already on screen costs a second. Put the waits in one helper file of the
target that checks the condition first (`exists || waitForExistence(timeout:)`). A wait that keeps
XCTest's, inside a measured window or watching for a change, says so with `// xctest-wait: REASON`
on its line or the line before. A UI test file is a tracked Swift file that imports XCTest and uses
`XCUIApplication` or `XCUIElement`, outside `evidence/`; its target is its nearest folder whose name
ends in `Tests`. These are warnings, which change no gate.

### Admission

Ten agents that each start a build at once make every build slow and every timing meaningless.
So every lane, and every command run through `qk admit -- COMMAND`, first asks a Mac for a
*slot*, in one first-come, first-served queue per Mac. A private pool of your own, such as
`mkdir` locks in a scratch folder, is a second queue that ignores load and that exclusive work
cannot exclude, so don't build one.

```sh
qk run LANE                      # a configured lane
qk run test -- --filter SUITE    # arguments after -- are appended to the lane's command
qk admit -- xcodebuild test -scheme SCHEME -destination "platform=iOS Simulator,id=UDID"
```

`qk admit` runs the command in the current directory with its output on the terminal, records a
receipt in `.build/queuekeeper/admit/`, and may run it on another Mac unless you add `--here`.
`--timeout SECONDS` bounds the wait for admission (5,400 s by default). With `qk admit --here`,
the command gets its full `--time-limit SECONDS` (2 hours by default) once it starts. If local
admission times out, no command starts and `qk admit` exits 75. For a
[bounded device run](#bounded-runs-on-a-device) that is already underway, waiting for admission
still reduces the time left in the device reservation.

Queuekeeper's own runners, `qk unittest`, `qk xctest`, `qk simulator-tests` and `qk unity test`,
take admission by themselves when started alone and reuse it when a lane or `qk admit` started
them. Any other command, such as a bare `swift test`, takes no slot unless run with `qk admit`.

Don't wait for a quiet Mac before starting work, and don't poll `uptime`, `top` or the load
average: on a busy Mac the load stays in the hundreds, so such a loop never fires. Admission waits
for a slot and chooses the Mac.

#### Choosing the request

| Request | How | For |
|---|---|---|
| One slot | `qk admit`, or a lane with `"resource": "heavy"` or none | Builds, tests, setup |
| Model | `qk admit --model`, or `"model": true` with `"resource": "heavy"` | On-device generation, MLX, Core ML that uses the GPU: one slot plus the Mac's single model lock |
| Whole host | `"wholeHost": true` on the lane or its group | A gate that runs the project's suites side by side |
| Exclusive | `qk admit --exclusive`, or `"resource": "exclusive"` | A timed or whole-machine measurement only |
| Self | `"resource": "self"` | A script that admits each of its own parts |

- **Only the timed part is exclusive.** Build and set up with `qk admit --here`, then run the
  timed part alone with `qk admit --here --exclusive`. Both need `--here` so they run on the same
  Mac. An exclusive request inside a one-slot admission is refused, so a lane that needs both is a
  `self` lane. A lane group can neither be `self` nor run a `self` lane.
- **A gate suite is never exclusive.** `qk admit --exclusive` refuses a command that is word for
  word a lane's `command` or `compileFirst`; run it with `qk run LANE`.
- **A check that voids itself on a busy Mac doesn't belong in a shared gate.** Admission judges
  only when a run starts. Give such a check its own exclusive lane, listed in `queue.lanes` after
  the gate, or admit it from a `self` lane with `--exclusive`.
- **Exclusive work settles first.** It holds every slot and the model, then waits up to five
  minutes for the CPU to read under 25% busy, and starts anyway at that bound. Its command then
  finds what admission read in `QUEUEKEEPER_HOST_BUSY`, `QUEUEKEEPER_HOST_LOAD`,
  `QUEUEKEEPER_HOST_SETTLED_SECONDS` and `QUEUEKEEPER_HOST_QUIET` (1 when the Mac read quiet). A
  check that must judge the Mac reads `QUEUEKEEPER_HOST_QUIET` rather than waiting on its own.
- **A model run that is timed** or compared with a baseline stays exclusive. Core ML pinned off
  the GPU (`.cpuAndNeuralEngine`, `.cpuOnly`) needs no model lock.
- **`qk doctor` warns** of a `self` lane whose command never calls `qk admit`.

Admission counts a lane as one job however many processes it starts, so bound its own
parallelism: `xcodebuild` takes a task per core unless given `-jobs N`, and a debug `swift test
--jobs N` is bounded. A release `swift test -c release` is not, whatever flags it gets. A script
that runs several jobs at once leaves at least one guaranteed slot for others.

#### A Mac's limits

Each Mac admits by its own limits:

- **Three guaranteed slots** open while the CPU's busy share is under the Mac's `busyLimit`.
- **More slots**, up to `slotsMax` (half the cores), open one at a time under the same limit,
  while memory pressure is normal and 30 seconds (`settleSeconds`) after the last one was taken.
- **At or over the limit** a Mac admits only its first job, so load that never asks can delay
  admitted work but never stop all of it.
- **A whole-host lane** starts once every guaranteed slot is free, and nothing else is admitted on
  that Mac while it runs. While it waits, later work may take only slots beyond the guaranteed
  ones, so they drain toward it.
- **Nothing is preempted.** Work admitted while the Mac was quiet may grow heavy later.

The busy share is the CPU's time outside idle over about the last 15 seconds. `busyLimit` is 90%
by default; setup (`python3 tools/install-checkout.py`) writes 70% for the Mac it runs on, never
changes a value already set, and `--check` prints the limit in force. `qk farm add` writes nothing
on the Mac it adds.

A Mac's limits live in `~/Library/Application Support/queuekeeper/admission-limits.json` on that
Mac, with the keys `slots`, `slotsMax`, `busyLimit` (a percentage) and `settleSeconds`. Each is a
number or `{"perCore": F}`, and a key left out keeps its default, as in
`{"slots": 4, "busyLimit": 85}`. A file admission can't read, or one with an unknown key, refuses
all work on that Mac with an error naming the file. To load a Mac more, raise `slotsMax` and
`busyLimit`, not `slots`: every guaranteed slot added makes a whole-host gate wait longer there.

- `qk admissions limits` prints this Mac's limits and where each comes from.
- `qk admissions usage` prints every Mac's limits, its reading now, and its share of time at each
  count of admitted jobs and under a whole-host lane.

An environment variable named for a limit, such as `HEAVY_BUSY_LIMIT`, overrides the file for one
process. On the shared queue, set it for every session or none.

Tests of admission can use a private slot directory with `HEAVY_SLOT_DIR`. The private queue still
measures the Mac's real CPU busy share. Once one slot is held, a busy Mac can keep a second job
waiting even when the private queue has a free guaranteed slot. To keep host CPU load from
blocking that slot, set `HEAVY_BUSY_LIMIT=101` in the test fixture's environment, or put
`{"busyLimit": 101}` in the private directory's `admission-limits.json`. The CPU busy share can
reach 100%, so it stays below that test limit.

#### Waiting

A wait is expected. A waiting request prints `heavy: waiting (Ns): REASON` when it starts and every
five minutes after, naming the limit that refused it, the limit's value and where it was set, and
the request it waits behind. The reasons are:

| Kind | Waiting for |
|---|---|
| `slots` | A free slot, or every guaranteed slot for a whole-host lane, or every slot for exclusive work |
| `settle` | The CPU to show the last job admitted, within `settleSeconds` |
| `cpu` | The CPU's busy share to fall under `busyLimit` |
| `memory` | Memory pressure to return to normal |
| `model` | The run holding the model lock |
| `exclusive`, `wholeHost` | Exclusive or whole-host work queued ahead or running |
| `stranded`, `guard` | An entry no running admission vouches for (see [Stuck admission entries](#stuck-admission-entries)) |

Arrival order holds across kinds: an older one-slot request runs before a later whole-host or
exclusive one. A lane offered to other Macs keeps its place here meanwhile. A lane run's label in
these messages is its project and lane, such as `EPUBLib gate`; a `qk admit` request's is its
command line.

When the wait times out, the request fails with nothing run and no lock reclaimed, naming the
reason, the Mac and what held it. When no Mac admits the work, the message gives another Mac's
reason and this Mac's.

#### Who holds and who waits

```sh
qk admissions now [--json] [--here]            # every Mac now: holders, waiters, reasons
qk admissions [--since WHEN] [--until WHEN]    # summarize this Mac's admission log
qk admissions --wait-breakdown [--since WHEN]  # wait estimates by slot scope and blocker
qk admissions waits [--since WHEN] [--kind screen]   # how often and how long lanes waited
```

`qk admissions now` asks every Mac at once and changes nothing. For each holder it shows the
slots, kind, hold time, CPU, and where the work comes from: repository, branch, worktree, agent
session and Mac; for each waiter, its wait and reason. A Mac it can't read within 10 seconds is
shown as unknown, never idle. A holder whose process has ended is shown as ended; admission frees
it at its next attempt.

`qk admissions --wait-breakdown` groups requests into whole-host, one-slot, and exclusive work.
It estimates time by blocker in proportion to each request's refused attempts. Whole-host requests
record whether the CPU was under its limit when all guaranteed slots were required and at least one
was busy. Older records can leave a withdrawal's scope or a blocker's details unrecorded.

A held slot from an `eng checkpoint` names the project, commit, current lane and its position
when the report can match the slot to the checkpoint's dedicated worktree. This also works for
a lane sent to another Mac when the report reads both Macs.

`qk admissions` summarizes each kind and label over a period: requests admitted and withdrawn,
their waits and what they waited behind, the loads seen and how admissions ended. Each Mac keeps
its own log in `~/Library/Application Support/queuekeeper/admissions.jsonl`; to read another
Mac's, pass a copy, the older generation first:

```sh
qk admissions <(ssh build-host \
  'cd "Library/Application Support/queuekeeper" && cat admissions.1.jsonl admissions.jsonl 2>/dev/null')
```

`qk admissions waits` covers the waits the admission log cannot see, for an unlocked screen,
another Mac's foreground, disk space or an offer, as well as for admission. Each Mac keeps the waits
that ended on it; the command reads this Mac's and asks the farm's other Macs, and gives each Mac's
count, total and longest wait of each kind, then each project's lane. The period defaults to the
last 24 hours.

#### Load admission cannot see

Admission reads a Mac before it admits and cannot count work that never asks:

- code navigation through `sourcekit-lsp`, which builds the worktree at full parallelism (agents
  navigate with `grep` instead);
- booting a fresh simulator, Xcode's indexing and an open Xcode's own builds;
- a long measurement that holds a checkout's SwiftPM build lock, which a `swift test` in that
  checkout then waits behind silently.

Never stop another session's process merely because it is busy.

#### Trial runs

To try Queuekeeper without touching the live system, set `QUEUEKEEPER_TRIAL` to a folder's full
path and run in a throwaway clone. Every `qk` command, and everything it starts, keeps its state
there: admission, the farm, the device record, kept app builds and pinned checkouts. A trial's
farm has no Mac, `qk doctor` fixes nothing, and the queue pushes to the clone's own `origin`.

### Process supervision

Each lane, `qk admit` command and runner runs in its own process group and session, with a time
limit, and Queuekeeper follows every process it starts.

- **When the command ends**, anything it left running has 60 seconds to end before it is stopped
  (SIGTERM, then SIGKILL 10 seconds later). Leftover Xcode helpers, such as `DTServiceHub`, are
  stopped at once. The command's own exit status still counts.
- **A lane's `timeout`** stops the command the same way. The run is unmeasured (exit 3), never
  green.
- **A stop.** SIGTERM, SIGINT and SIGHUP stop the command and everything it started, with SIGKILL
  10 seconds later. A stop while still waiting for admission leaves nothing behind.
- **Uncertain runs.** After a failure, stop or timeout, Queuekeeper looks for anything still
  running in the command's process groups and sessions. If something survives, the run is marked
  uncertain and its slots stay held until that is settled, so leftover load is never hidden. A
  passing command is not checked, so a daemon it starts on purpose holds nothing.

#### Deadlines and stopping commands

Keep three deadlines apart: your tool's wait for output, a connection or launch timeout, and the
test's own deadline. A tool that returns before the command ends has observed nothing about the
command:

- **Follow an admitted command to its result.** When your tool's wait expires, the command is
  usually still running: resume it or read its log and `qk progress`, and don't start a duplicate.
  Wait for a confirmed result before starting dependent tests or pushes.
- **Never stop an admitted command's task to retry or end it early.** An agent tool's stop often
  sends SIGKILL a second or so after SIGTERM, before Queuekeeper has stopped what the command
  started; the run is then left uncertain, holding its slots, and its command may run on
  unsupervised. To end a run early, stop the command's own processes and let Queuekeeper follow
  them to a result.
- **Start long admitted commands in the background**, so a foreground timeout never cuts them off.
- **Before ending a session or a subagent**, let its admitted commands finish. Stop only commands
  still waiting for admission, which hold nothing. Archiving or deleting a Claude Code session,
  quitting the Claude desktop app, ending a Codex session, or archiving a Codex Desktop thread
  stops its commands.

The [developer guide](developer_guide.md#how-agents-tools-stop-commands) says what each tool's
actions do.

#### Stuck admission entries

An admission whose owner has ended, or can't be confirmed running, holds nothing: admission stops
what is left of it and frees its slots at its next attempt. An entry left *uncertain* by a stop is
recovered automatically once its owner has ended and nothing it ran still runs, checked every
minute. Until then it counts as the slots it held, and other work admitted beside it says so.

```sh
qk admissions recover                       # list dead or uncertain entries
qk admissions recover PID                   # recover one, by owner PID or identity
qk admissions recover --host build-host PID # one on another Mac
qk admissions remove PID --why "WHAT YOU CHECKED"
```

`qk admissions recover` changes an entry only when it can prove the owner and everything it ran
have ended; otherwise it exits 1 naming each process still running. Stop or wait for those, then
run it again. If the connection to another Mac drops, list there again before acting.

An entry recovery can't prove finished, such as one with no record of its commands, is for a
person: check with `ps` that nothing its owner started still runs, then remove it with
`qk admissions remove`, which checks what it can first and records your `--why`. `qk queue status`
and `qk queue doctor` say when a gate is stuck behind such an entry.

## The build farm

The build farm is the Mac you work on plus any other Mac you can reach over SSH.

### Adding and removing Macs

```sh
qk farm add DESTINATION [--name NAME]   # add a Mac, last in the order
qk farm list                            # the Macs, in the order work is offered to them
qk farm rename NAME NEW
qk farm pause NAME                      # offer it no work until `qk farm resume NAME`
qk farm remove NAME                     # take it out for good
qk farm foreground NAME [--off]         # allow lanes that take its screen, or not
```

The list lives in `~/Library/Application Support/queuekeeper/farm.json` on the Mac that sends
work. Nothing else names a Mac: no SSH alias or environment variable is read in its place. A farm
with no Mac that takes work runs every lane on this Mac.

- `qk farm add` reaches the Mac first and asks for its hardware UUID and local host name. The
  UUID tells Macs apart, so this Mac, or a Mac already listed under another name, is refused.
- A Mac's history follows its hardware, not its name, so renaming it is safe.
- A paused Mac stays listed, is offered nothing, and finishes what it is running.
- Removal is refused while work this Mac sent is still running there; pause it meanwhile. Its
  checkouts stay on it until you delete them, and earlier records of it are kept.

Messages and receipts name a farm Mac by its name in `qk farm list`. Older records may call
another Mac the *build host*; they still count.

### Setting up a Mac

The owner prepares each added Mac once:

1. **Remote Login and SSH.** Turn on Remote Login (System Settings, General, Sharing) and add this
   Mac's public key to that Mac's `~/.ssh/authorized_keys`.
2. **An SSH alias** in this Mac's `~/.ssh/config`, so that `ssh ALIAS true` succeeds without a
   prompt, then `qk farm add ALIAS`:

   ```
   Host ALIAS
       HostName NAME.local
       AddressFamily inet
   ```

   `AddressFamily inet` keeps ssh on IPv4. A `.local` name often resolves first to an IPv6
   address that never answers, and every connection then waits out ssh's 10-second timeout.
   `qk doctor` reports a first address that keeps connections waiting.
3. **Xcode.** Install the same Xcode build this Mac uses, at the same path. A Mac with a different
   Xcode at this Mac's developer directory refuses work.
4. **Homebrew and tools.** Install Homebrew there yourself (its installer asks for that Mac's
   password), then run `qk remote setup` from this Mac (see Tools, below).
5. **Simulator runtimes**, for Apple projects: `qk remote setup --runtimes` (below).
6. **Power.** Keep it on power and awake. A sleeping Mac leaves its run unmeasured, and its
   `.local` name resolves only while it is awake on this Mac's network
   (`dscacheutil -q host -a name NAME.local` shows whether it does).
7. **A GitHub token**, if lanes fetch private sources (below).
8. **Keychain**, if its runs sign: `qk keychain keep-unlocked --target MAC`
   ([Keychains on another Mac](#keychains-on-another-mac)).
9. **Plugin folders.** Clone each folder the project's plugins load from beside it
   ([Plugin folders beside a project](#plugin-folders-beside-a-project)).

Then run `qk doctor` and a short lane there, such as `qk remote run LANE --host NAME`.

#### Tools

```sh
qk remote setup [FORMULA ...] [--import MODULE ...] [--host NAME] [--check]
```

It puts Homebrew on the login shell's `PATH` in `~/.zprofile`, since work there runs in that
Mac's login shell, and installs Homebrew's Python, `uv`, `shellcheck`, `xcodegen`, `zstd` and
each formula you name. A new login shell then checks every tool, each `--import` module and the
Xcode, one line each, and the command fails while anything is missing. Without Homebrew it stops
with nothing changed; `--check` only checks. It also checks for the project's plugin folders
and names the `git clone` to run, but clones nothing.

#### Simulator runtimes

```sh
qk remote setup --runtimes [--check]
```

Run it at setup and whenever either Mac gains a runtime; `qk doctor` runs it itself when the
runtimes differ. It makes the Macs hold the same runtimes of this Mac's Xcode:

- A runtime on one Mac only that nothing uses (no pin and no device record names it) is deleted
  there. One that is used is copied across over SSH as a disk image, so a beta Apple no longer
  serves arrives exactly; only an unreadable image is downloaded instead.
- It deletes nothing while any simulator runs on that Mac, since that can crash them all, and
  exits 1 naming them. Run it again once none runs.
- It refuses a copy that would leave less than 40 GiB free.
- It exits 0 only when the runtimes match. Five runtimes move about 40 GB, so run it in the
  background; its last line is the outcome.

#### Private sources

Project commits reach another Mac over SSH with no GitHub credential. Fetching private toolkit
code or private Swift package dependencies over HTTPS still needs one, and git's keychain helper
cannot read the keychain over SSH. Create a fine-grained GitHub token with read-only Contents
permission on the private repositories, and write it on the other Mac without leaving it in the
shell's history:

```sh
read -rs "token?Token: "; echo
(umask 077; printf 'https://USER:%s@github.com\n' "$token" > ~/Development/queuekeeper-farm/git-credentials)
unset token
```

Each run's git reads that file through the store helper alone; your own git configuration there
is unchanged. Renew the token before it expires. Never put a token in a package manifest, URL or
committed configuration.

#### When macOS renames a Mac

macOS renames a Mac whose `.local` name another machine claims, adding or raising a `-N` suffix,
and the old name stops resolving. When a name does not resolve or answers with another host key,
Queuekeeper searches the Macs Bonjour lists for the one presenting the host key `known_hosts`
holds, uses it if exactly one does, and says so on a line of its own. A search that finds no Mac
presenting the key says nothing, and the failure is reported in ssh's words alone. `qk doctor`
reports the rename until you fix it: put the new name in `HostName` and the old one in
`HostKeyAlias`, so a later rename needs only a new `HostName`. `sudo scutil --set LocalHostName
NAME` on that Mac, with a name nobody else will claim, makes renames rarer.

### Which Mac runs a lane

Every lane, a short check as much as a gate, is placed among the farm's Macs, this one included.
Queuekeeper decides, not the agent: don't ask anyone which Mac to use.

Each Mac is read for what its own admission would admit now. Of the Macs with room, the one with
the most *headroom*, its CPU limit less its CPU's busy share, takes the work. A Mac 40% busy
against a limit of 90% has 50 points; against a limit of 70%, 30.

- **Each Mac's limit is its own.** Because your Mac's limit is 70% and an added Mac's is 90%, the
  added Mac takes the work at equal load. Change a Mac's `busyLimit` to load it more or less.
- **Ties** go by the farm's order, with this Mac after the listed Macs.
- **Room** means a free slot, the CPU under its limit (or nothing admitted running there), and no
  whole-host gate or exclusive work running or waiting ahead.
- **No Mac with room**: the lane waits in this Mac's queue and is offered again, every 15 seconds
  at first and at most every two minutes, and runs on whichever Mac admits it first. A lane no
  Mac admits within 90 minutes ends with exit 75.
- **Exclusive work** sent to another Mac waits up to 15 minutes for a moment it can start at once
  before it joins that Mac's queue, so that Mac keeps taking other work meanwhile.
- **A gate** that runs its own suites side by side says `"wholeHost": true` and holds a whole Mac.

A placement line says where a lane went and why, for example
`queuekeeper: trying LANE on this Mac: this Mac has the most headroom ...`, and a refusal names
its cause and where the Mac sets that limit.

**Lanes that stay on this Mac:**

- `qk run provision`, which prepares the checkout in hand. `qk remote run provision` explicitly
  prepares a checkout on another Mac.
- a lane that says `"here": "REASON"`, such as one needing a physical device or this Mac's GPU
  (the reason is required);
- a `self` lane, and the `qk admit` commands its script runs, since its parts share build
  products;
- a group, when it or one of its lanes says `here`;
- `qk run --here`, `qk admit --here`, `--serial`, and work nested inside an admission.

The receipt's `hereBecause` says why a run stayed. Simulator and background UI lanes go to
other Macs like the rest; a lane whose test counts differ on another Mac has a defect to fix, not
a reason to stay. The old lane setting `"buildHost"` is refused.

To see where a gate would go without running one:

```sh
qk queue placement [LANE] [--json]   # each Mac's capacity now, and why it would or would not start the gate
qk queue placement LANE --worktree   # the same, by this checkout's uncommitted queuekeeper.json
qk queue placement LANE --ref REF    # the same, by the queuekeeper.json at REF, such as a branch
qk admissions usage                  # how busy each Mac has been, and why work ran here
```

A reading is a snapshot; the Mac checks admission again when the lane starts. By default
`qk queue placement` reads the lane from `origin/main`, as the queue does, and says so when the
working tree changes the lane's placement (its `labels`, `resource`, `wholeHost`,
`unlockedScreen`, `foreground`, `model` or `here`). To check such a change before it lands, run
it with `--worktree` or `--ref`. The first line names the file it read and the labels the lane
needs; `--json` gives them as `source` (`main`, `worktree` or `ref`), `config` and `labels`.

### What a Mac takes

A Mac's own settings say which kinds of work it takes and which labels it has; a lane says which
labels it needs. No lane names a Mac, and no Mac names a lane. The settings are
`~/Library/Application Support/queuekeeper/mac-settings.json` on that Mac:

```json
{
  "takes": {"wholeHost": false, "foreground": true},
  "labels": ["unity-license", "apple-m5-max"]
}
```

- **Kinds.** `takes` may set `wholeHost`, `exclusive`, `model` and `foreground`. Left out, a Mac
  takes whole-host gates, exclusive work and model runs, and no foreground work. A workstation
  that says `"wholeHost": false` runs no project's gate.
- **Labels** are lowercase words joined by hyphens, such as a license or a chip. A lane lists
  what it needs in `"labels"` and runs only on a Mac that has every one; a group needs its lanes'
  labels as well as its own. A lane whose results hold only on one chip, such as one that compares
  rendered images byte for byte, needs a label naming it, such as `apple-m5-max`.
- **Foreground.** A Mac marked `"foreground": true` lends its screen to foreground lanes without
  a window agreed beforehand. `qk farm foreground NAME` writes the mark, `--off` removes it, and
  `qk farm foreground here` marks this Mac. While no reachable Mac takes it, a foreground lane is
  refused, naming the command that fixes it.
- **Declining.** A Mac that declines a lane is not offered it and gives it no place in its queue,
  and the placement line says why, for example
  `studio lacks the label unity-license that the work needs`. When no Mac takes the lane, nothing
  runs and it exits 78, naming each Mac's reason. The merge queue counts it as a run without a verdict.

`qk admissions limits` prints what a Mac takes, its labels and the file's path. `qk admissions usage`
prints the same of every Mac beside its capacity, with the keys it passes over and where foreground
lanes go, and `qk doctor` warns of a lane no Mac takes, naming what it needs and each Mac's reason.
Doctor reads the lanes from the working tree's `queuekeeper.json`, so it checks a lane change
before it lands, as `qk queue placement LANE --worktree` does. A
Mac that cannot be read is unknown in both, never one that lacks. Doctor reads the Macs at once,
and only when some lane needs something a Mac may decline; one that does not answer within 10
seconds, such as one asleep, is unknown. Edit the file by hand. An unknown key is reported as `ignored:`; a file that is not JSON, or a known key of the
wrong type, stops placement on that Mac rather than guess.

### Commands and where they run

```sh
qk run LANE [-- ARGUMENTS]               # the Mac with room and the most headroom
qk run --here LANE                       # this Mac, for products the session uses here
qk run --offload-only LANE               # another Mac if one can start it now, never here
qk run --offload-wait LANE               # wait for admission on another Mac, never here
qk admit -- COMMAND                      # any other heavy command, placed as a lane is
qk admit --here -- COMMAND               # this Mac, for a build whose output the session uses here
qk remote run LANE [--host NAME]         # a lane, on another Mac
qk remote admit -- COMMAND               # a command, in the root of a checkout on another Mac
qk remote passed LANE [--commit REV]     # exit 0 if the whole lane passed at exactly that commit
```

- **`--here` is not for a busy Mac.** Use it only when the session needs the products here
  afterwards: build output, DerivedData, a result bundle, an app it launches. A test a lane covers
  runs best as `qk run LANE -- FILTER`.
- **`--offload-only`** is for a caller that holds admission here already. It exits 75 when no
  other Mac is configured, reachable or free, so the caller runs the lane itself, and 78 when
  another Mac is misconfigured.
- **`--offload-wait`** waits for another Mac to admit the lane. The merge queue uses it for a
  newer bisect half or stacked gate whose companion gates here. With several eligible Macs, it
  retries the offer while they are busy. It exits 75 when none is configured or reachable, or the
  admission wait expires.
- **`qk admit` stays here**, saying why, when another Mac could not run the command as it runs
  here: its directory is outside the checkout or ignored by Git; its arguments name a path only
  this Mac has (this checkout's absolute path, `/tmp`, a scratchpad, anything under `~`, an
  existing ignored file such as a scratch script, a pool simulator or a physical device); or the
  environment sets `DEVELOPER_DIR` or a `TEST_RUNNER_` variable. System paths such as `/usr` and
  `/opt/homebrew` do not count.
- **Redirected standard input.** `qk admit` keeps commands fed by a pipe, a here-document or an
  input file on this Mac and passes that input to the command. Closed input and `/dev/null` still
  allow offloading. A command explicitly sent with `qk remote admit` gets no caller input. To use
  a file on the other Mac, include it in the checkout and redirect from it inside the command.
- **A named checkout.** `qk remote run LANE --checkout-slot N` (0 to 3) runs in that checkout and
  no other, for a check that depends on its path or warm products. It waits for that checkout,
  up to `--checkout-wait SECONDS` (default 5400), and exits 75 having run nothing if it runs out.
- `qk remote` always means another Mac: the first in the farm that takes work now, or the one
  `--host` names by farm name or SSH destination. It takes no slot here.

### How the code gets there

The exact commit is pushed over SSH straight to the other Mac; nothing goes through the Git host.
With uncommitted changes, Queuekeeper sends a snapshot commit of the working tree instead,
leaving your branch, index and HEAD untouched. Ignored files stay behind, so ignore large files
another Mac does not need. Queuekeeper's own code goes the same way.

**A snapshot's result is no commit's pass.** `qk run` says so as it sends one, naming the changed
files, and `qk remote passed` never counts it. Commit and run again for a pass that counts.

**A run on this Mac reads your checkout's live files.** Unlike a run sent to another Mac, it takes
no snapshot, so keep the checkout still while it runs, and start other work in another worktree.
Queuekeeper reads the checkout's commit and tracked changes as the lane's command starts, and again
when the run, or each lane of a group, ends:

- **The checkout moved to another commit**, as after `git switch`: the run is unmeasured (exit 3),
  since it read some of each commit. The message and the receipt's `checkoutMoved` name both
  commits, and the run is never kept as either commit's pass or test count. A group says so as
  soon as one of its lanes ends.
- **Tracked files changed at the same commit**, as after `git checkout other -- FILE` or a build
  that rewrites a tracked file: the run keeps its verdict, which is what it read, but is no
  commit's pass. The message and the receipt's `checkoutChanged` name the files.
- **The checkout moved while the run waited for a slot**: the run reads the new commit, says so,
  and its receipt names the commit it read.

Untracked files are not compared, since a lane's own outputs land there.

Each Mac keeps four checkouts per project, under `~/Development/queuekeeper-farm`, and a run takes
the free one closest to its commit, so builds stay warm. The project's `provision` lane, if it has
one, runs first in that checkout, and the work runs only if it passes. A checkout a run left with
changes is passed over and kept for inspection; the message names the files and the command that
removes it.

**Environment variables do not travel.** Another Mac runs work in its own login shell's
environment. Pass lane settings as arguments after `--`, such as
`qk run purchases -- --runtime ID`, and confirm them in the receipt. What a project's plugins
add to every command's environment does reach it. The Apple plugin names
caller-set `TEST_RUNNER_` variables after an applicable remote lane runs. This covers lanes that
invoke `xcodebuild test`, `xcodebuild test-without-building`, or `qk simulator-tests` directly
and lanes that declare `idioms`. The notice gives names only. A value set explicitly by the
lane's command remains part of that command. Use `qk run --here LANE` when a lane needs this
Mac's environment.

**What comes back:**

- The output, streamed as it is written. The log and receipt land in
  `.build/queuekeeper/remote/LANE/` (`remote/admit/` for a command), a group's lanes' logs in
  `lanes/` beneath it.
- The exit status of the work there.
- For `qk admit` and `qk remote admit`, what the command changed in files Git does not ignore, as
  a patch applied to your working tree while it is still the tree that was sent. Otherwise the
  patch is kept in `.build/queuekeeper/remote/unapplied/` and the command exits 73. Build
  products stay on the other Mac; use `qk admit --here` when you need them.
- Simulator lanes run headless from that Mac's own simulator pool; only logs and receipts come
  back. Run the lane with `--here` to get a result bundle or device log here.

The receipt names both Macs' model, macOS and Xcode, and `ranOn` says where the lane ran.

Once the sender has acknowledged a run's journal, a run without a device lease can continue on
the other Mac if your Mac sleeps or the connection drops. The sender reconnects to collect the
same run's result. Stopping a run here asks the other Mac to stop it, including on reconnection.
A run with a device lease, or whose journal was never acknowledged, stops when its connection
ends or the sender is silent past its bound.

**Reading an uncollected journal.** If the sender reports an unmeasured run and names a retained
journal, use the host and journal ID from that message to copy its frame file into a scratch
directory with room for the log and returned files. This reads a snapshot while leaving the
running or finished dispatch alone.

```sh
ssh HOST 'cat "$HOME/Development/queuekeeper-farm/dispatches/JOURNAL_ID/frames"' > dispatch.frames
grep '^R ' dispatch.frames
```

Check that the journal announcement (the `J ` line) names the expected ID. The terminal `R ` line
contains JSON. Compare its host, checkout and receipt's project revision and arguments with the
host and work originally sent. A matching terminal result gives the remote exit status and
whether the run was stopped or refused. An absent, incomplete or mismatched result gives no
verdict. This copy neither acknowledges the result nor requests a stop, and does not turn the
local unmeasured receipt into a pass.

If a lane this Mac takes finds the working tree changed since the commit was sent, it runs the
commit sent in a temporary worktree, saying so, and removes the worktree afterwards, so its
builds start cold.

### Unreachable and misconfigured Macs

Before sending anything, `qk run` checks that the Mac can be reached: its name resolves, its ssh
port answers within 3 seconds, and `ssh HOST true` finishes within 30.

- **Unreachable** is not a configuration problem. The lane goes to this Mac's queue at once, and a
  line says why. An offer that misses once, the first included, is retried within 30 seconds; only a second miss in a row counts
  the Mac unreachable.
- **Misconfigured** stops the lane for the owner, with exit 78 and the fix: a refused login or
  changed host key, a failing login shell, a different Xcode build, every checkout left changed, a
  failing `provision` lane. In the merge queue that is a run with no verdict.
- **Lost after sending**: the run is unmeasured rather than run again here, since it may still be
  running there.

A failed connection reports what ssh said, when the run was sent and lost, and how long this Mac
slept in between. A Mac asleep runs no ssh, so a timeout this Mac slept through says nothing about
the other Mac; keep this Mac awake. ssh drops a connection after about two minutes of silence.

| ssh says | Meaning |
|---|---|
| `connect to host ADDRESS port 22: Operation timed out` | Nothing answered within 10 seconds |
| `Connection timed out during banner exchange` | The port answered, but no SSH banner came within 10 seconds |
| `Connection to ADDRESS port 22 timed out` (no banner line) | Silence for two minutes during key exchange or login |

`qk doctor` compares the Macs and reports as errors a different default Xcode, a different Xcode
at this Mac's developer directory, different simulator runtimes (which it evens out itself), a
refused login, and leading addresses that do not answer. An unreachable Mac is `unchecked`, not an
error. It also shuts down stale pool simulators on each Mac and keeps each Mac's own Queuekeeper
checkout current when it safely can.

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

Any Mac can lock, by hand or after idle time. Queuekeeper never assumes a screen's state: it reads
it on the Mac that runs a lane and records what it read, or `unknown`. `qk host screen` reads this
Mac's (`--remote` another's) and exits 0 unlocked, 1 locked or 2 unknown.

Most lanes run fine behind a lock. A lane whose tests need an unlocked screen, such as one that
reads its own windows through the accessibility API, says `"unlockedScreen": true`, and so does
every group that runs it. Such a lane:

- **runs only on a Mac whose screen reads unlocked.** Locked and unknown both count as no room,
  and the placement line says which Mac was locked.
- **waits while none is**, and is offered again like any waiting lane, instead of running
  unmeasured. A gate reads the screen again once admitted; if it locked meanwhile, the gate gives
  back its admission unstarted and waits again.
- **has the screen read and logged** as it starts (`queuekeeper: screen: STATE ...`), kept in its
  receipt as `screen`, and read again as `screenAfter` when it ends unmeasured.

`"display": true` wakes the display and keeps it on, but unlocks nothing. A `"display": true`
lane whose tests can't measure behind a lock also says `"unlockedScreen": true`; one that can says
`"unlockedScreen": false`. `qk doctor` warns about a display lane that says neither.

A run that ended unmeasured because the screen locked is not held against the commits and does
not count toward a hold. The queue runs that lane next with `qk run --unlocked-screen`, which
places it as if it declared `"unlockedScreen": true`. Queuekeeper cannot unlock a screen.
`qk admissions waits --kind screen` shows how often and how long each lane waited for one.

### macOS Local Network privacy

macOS's Local Network privacy judges a connection by the process it holds responsible. A
session's queue worker inherits the session's permission, but once that session's app process
exits, as when the session is archived, every local-network connection the worker makes fails at
once with `No route to host`, while other sessions still reach the other Mac.

Queuekeeper detects this rather than reporting the other Mac as down:

- **The queue worker** checks before each round, and on finding itself cut off exits, holding
  nothing, with its entries back in the queue. The next `qk land`, `qk queue wait` or
  `qk queue resume` from a live session starts a worker that can connect. A session that finds
  the queue waiting with no worker runs `qk queue resume`.
- **A gate's lane** cut off this way runs nothing and exits 69, which the queue treats as no
  verdict without counting it toward a hold. A session's own `qk run` still falls back to this
  Mac.

The signed queue launcher avoids the problem: a LaunchAgent runs a signed app that macOS holds
responsible for the worker.

The [Apple plugin](../plugins/apple/src/queuekeeper_apple/README.md) keeps Xcode and simulator
runtimes alike across Macs.

### Keychains on another Mac

A keychain unlock reaches one Mac and only the login session it was made in. Unlocking your own
Mac does nothing for another, and an unlock made in Keychain Access or an earlier `ssh -t` session
does not reach a `qk remote` run, since every run is its own SSH login. So each run unlocks the
login keychain itself, from a password the owner gives once:

```sh
qk keychain keep-unlocked [--target MAC] [--identity NAME]
```

The owner runs it in their own Terminal, once for each farm Mac whose runs sign. On that Mac it
asks for the login password at its own prompt (three tries), keeps the one that works in
`~/Development/queuekeeper-farm/keychain-password`, readable only by the owner's account, and stops
the keychain locking on a timer or on sleep. It exits 0 when done, 1 when no password worked, and
2 when it refused, as on the wrong Mac, without a terminal, or before any `qk remote` run has set
up that Mac. Each `qk remote run` and `qk remote admit` then unlocks the keychain in its own
session. When the password stops working, runs are refused until you run `keep-unlocked` again.
To undo it, delete the file and set the lock settings with `security set-keychain-settings`.

Two more commands help:

```sh
qk keychain recover [--target MAC] [--keychain PATH] [--identity NAME]
qk remote admit -- python3 scripts/qk keychain check [--keychain PATH] --identity NAME
```

- **`qk keychain recover`** says which Mac's keychain the work needs, checks the keychain file is
  there under its exact name, and prints unlock commands that each run only on the Mac they name.
  It unlocks nothing and reads no password.
- **`qk keychain check`** runs where the signing runs and reports the session kind (`ssh`, `gui`
  or `background`), whether the keychain is unlocked, whether the identity is there, and whether
  `codesign` can sign with it. It exits 0 when signing passed, 1 when it failed or the keychain
  or identity is missing, and 3 when unmeasured.

A result holds only for the kind of session it was measured in: signing in your desktop session
says nothing about signing over SSH. Never ask an owner for a password or put one in chat, a
command's arguments, an environment variable or a log. Partition lists, trust settings and
certificates are the owner's to change; neither command touches them.

### Plugin folders beside a project

A project's plugin may load from a folder beside it, named in `queuekeeper.json` as
`{"module": "NAME", "path": "../FOLDER/..."}`. Other projects share that folder and landings move
it, so a run pins it: every lane of a `qk run`, a `qk remote run` or a queue round, on any Mac,
loads the folder at the commit its `origin/main` named when the run started, never your
checkout's branch or changes. `qk run` names the commit as it starts and the receipt records it.
Other commands, such as `qk doctor` and `qk admit`, load the folder as it stands.

- **Folders it loads from in turn.** A folder's own `queuekeeper.json` may list further folders
  under `siblings`, each with the variable its code finds it by:
  `"siblings": {"apple-engineering": "APPLE_ENGINEERING_ROOT"}`. They are pinned the same way.
- **Testing a newer commit of the folder.** `qk run --sibling FOLDER=WHERE LANE` pins it at a
  commit or a checkout's path instead. A commit no branch of the origin holds runs on this Mac
  only. A change that needs an unlanded folder change is two landings, the folder's first.
- **Another Mac needs its own checkout** of each folder before you list the plugin in
  `queuekeeper.json`. Clone the same origin into `~/Development/FOLDER` on that Mac.
  `qk doctor` and `qk remote setup` name the clone. Without that checkout, or when the Mac
  cannot fetch the commit, the lane ends with exit 70, `queuekeeper: could not start:`, and the
  queue holds for the owner.
- **A pin set that does not start** in the project is not adopted: the queue keeps the last set
  that worked, and with none it holds.

Pinned checkouts live in `~/Development/queuekeeper-pins/` on each Mac; `qk disk` lists them and
`qk disk --prune-pins` removes those unused for a week. A folder that is not a Git checkout loads
as it stands.

### Running a Queuekeeper module in a child process

A project's script or helper that runs a Queuekeeper module as a process of its own, such as
`python3 -m queuekeeper_apple.devices check --owner OWNER`, asks Queuekeeper for the child's
`PYTHONPATH` rather than building it. Queuekeeper's packages sit in several folders, and a module needs
the plugins its own plugin requires too, which change as plugins split: `queuekeeper_apple` has needed
`queuekeeper_mobile` since the mobile plugin moved out. A path built from the packages a caller knows
of breaks with `ModuleNotFoundError` when that happens.

```sh
PYTHONPATH="$(qk python-path queuekeeper_apple.devices)" python3 -m queuekeeper_apple.devices check --owner OWNER
qk python-path --json queuekeeper_apple.devices queuekeeper.processes   # the path and its folders
```

`qk python-path MODULE...` prints this Queuekeeper's folders for the core, each module's package and
every plugin it requires, joined as `PYTHONPATH` takes them, and exits 2 for a package it cannot find.
Python code that has imported Queuekeeper calls `queuekeeper.checkout.python_path(*modules)` for the
same value, or `python_roots(*modules)` for the folders.

## Apps, devices and simulators

The platform plugins add what an app project needs beyond lanes: reservations of physical devices and
simulators, runs that take a Mac's screen, kept builds of the app, and Unity editors. Each plugin's README
says what it adds: [Apple](../plugins/apple/src/queuekeeper_apple/README.md),
[mobile](../plugins/mobile/src/queuekeeper_mobile/README.md) (the device record and device pass, which
the Apple and [Android](../plugins/android/src/queuekeeper_android/README.md) plugins share),
[Mac desktop](../plugins/mac-desktop/src/queuekeeper_mac_desktop/README.md) and
[Unity](../plugins/unity/src/queuekeeper_unity/README.md).

Everything about physical devices below applies to an Android phone too, except that it is named by its
adb serial rather than a UDID, and its notifications are silenced with Do Not Disturb over adb, with no
shortcuts to set up.

### Physical devices

A paired iPhone or iPad, a simulator pool's members and a Mac's foreground are used only under a
reservation in the Mac's one **device record**:
`~/Library/Application Support/queuekeeper/device-handoff.json`, outside every repository.
`qk devices` reads and changes it. The Mac desktop plugin provides it, so every project that
takes a Mac's foreground has it, a Unity project included; the Apple and Android plugins add the
actions for physical devices (`take`, `declare`, `defer`, `import`, `notifications` and `run`).

```sh
qk devices status                 # every entry, overdue ones, declarations and what each Mac reaches
qk devices show iPhone            # one entry and its revision (0 if new)
qk devices reserve iPhone --owner SESSION --revision N --device UDID \
  --purpose "…" --release-by 2026-10-21T18:00:00Z
qk devices update iPhone --owner SESSION --revision N --own PID --launched BUNDLE
qk devices release iPhone --owner SESSION --revision N --outcome "…" --restoration "…" --apps-quit "…"
```

- **Every change names the revision you read.** A change is refused if the entry has moved on or is
  no longer yours; a refusal changes nothing, so read the entry again and decide afresh. Each change
  prints the new revision for your next one.
- **Name a device by its uppercase UDID**, never by a name or `booted`.
- **Record what you start.** `update --own PID` records a process you launched, and `release` waits
  until it has exited. `update --launched BUNDLE` records an app you launched on the device; releasing
  then requires `--apps-quit` saying how it was quit.
- **`--owner` names your session.** Where it is optional (`take`, `run`, `check`, the notification
  commands), it defaults to `QUEUEKEEPER_DEVICE_OWNER`, then the agent's session.
- **Outside such a project**, run it through Queuekeeper's own checkout, as its recovery messages do:
  `python3 ~/Development/queuekeeper/bin/qk --project ~/Development/queuekeeper devices …`.

#### Taking a device

`qk devices take` waits in line for the paired iPad or iPhone, first come first served, one holder at a
time, and reserves it until `--release-by`:

```sh
qk devices take iPad --purpose "…" --release-by 2026-10-31T09:00:00Z
```

- It says who holds the device and until when while it waits, and gives up after `--wait` seconds (an
  hour by default), reserving nothing. Run it in the background when the wait may be long.
- It refuses a device the record has never seen (its first `reserve --device` records it), an
  uncertain entry, and an orphaned one Queuekeeper cannot recover by itself (see
  [Restoring, releasing and recovering](#restoring-releasing-and-recovering)).
- The record records no approval. Whether a session may use a device is between it and the owner: take a
  device the owner has approved standing use of whenever it is free; for any other, such as a personal
  phone, ask first and set `--release-by` to the end of the time agreed.

**The reservation is the window.** A device is held until `--release-by` and no longer for running
work, but it is released only explicitly: a passed release time, an idle screen or a silent session
proves nothing. `status` reports an overdue entry and never releases it; only the owner's notifications
are put back once it is well overdue ([An overdue silencing](#notifications-during-a-run)). Release yours
rather than holding it through offline analysis.

#### Bounded runs on a device

Run a command on a device you hold through `qk devices run`, which stops it before the reservation
ends:

```sh
qk devices run iPhone --owner SESSION --revision N --limit 180 -- \
  xcodebuild test-without-building … -destination "platform=iOS,id=UDID"
```

- It requires that you hold the entry with no run active, and refuses a command that names any other
  device, running nothing.
- The bound is the reservation's remaining time, less 105 seconds for the run's own end and
  `--restoration` seconds (60 by default) for your restoration checks, and at most `--limit`. A bound
  under 30 seconds is refused. The bound counts `xcodebuild`'s start-up and any time the Mac sleeps.
- It records the run's process and outcome in the entry: `exit N`, a timeout (status 124), a stop, or a
  command that did not start.
- A command that exits 0 having run no test (every one skipped, or a count of zero) returns 3,
  unmeasured. `--no-tests` keeps 0 for a run meant to select none.
- It never releases the device, changes VoiceOver, uninstalls apps or deletes data. Read the new
  revision with `qk devices show KEY` before your next change.

A project's own launcher can do the same from Python with `devices.BoundedRun`.

#### Restoring, releasing and recovering

- **Before releasing**, verify the original accessibility and keyboard settings, that none of your
  test processes remain, and remove only your own app and runner bundles. An interrupted test skips
  its own restoration, so query the settings yourself. If you cannot, record the restoration as
  unverified and leave the entry for recovery.
- **`release` refuses while a run is active.** Record the run's end first.
- **A run whose process is gone is recovered by itself.** A run whose recorded process has ended, or
  whose PID now names a process with another start, is an orphan. Queuekeeper ends it without being
  asked: it stops the command the run recorded if it still runs, puts the owner's notifications back if
  a restore is pending, ends the run, and records each step in the entry's `runRecoveries`. The
  reservation stays with its holder. The next pass or `devices run` on the device, `devices take`,
  `devices release` by its holder, `devices status` and `qk doctor` do this first, and so does an
  upkeep that the commands reaching the hosts (`qk run`, `admit`, `queue` and the like) start apart from
  themselves when one is due. Apps the run launched are left for the holder's release to quit, and a
  pass on a held device silences the notifications again before it runs.
- **Orphaned and uncertain entries.** An entry marked `uncertain` or `restoration-unverified` is
  uncertain. It, and an orphan whose end cannot be proven (no process or start recorded, a run sent to
  another Mac) or whose automatic recovery failed at a step, which `status` names with its command,
  cannot be reserved again until its owner releases it or someone the owner agreed records an attended
  recovery, once the work has stopped and the device is verified:

  ```sh
  qk devices recover iPhone --by SESSION --revision N --verified "WHAT WAS CHECKED"
  ```

  Recovery refuses while the recorded process still runs. For a run sent to another Mac, it first asks
  that Mac and refuses while any process there names the device's UDID or was left over from the run.
  An unreachable Mac keeps the device until the owner recovers it with `--offload-unchecked REASON`.

#### Devices and another Mac

**Physical devices are reserved only in this Mac's record**, the Mac that sends work to the others.
Every paired device is reachable from every Mac, so a record on each would let two runs share one.
Every `qk remote` run first makes the other Mac's record defer physical devices to this one's
(`qk devices defer MAC`); a deferring record refuses to reserve one. `qk doctor` reports each Mac's
record and calls it an error for this Mac's record to defer, or another Mac's to hold a device.

A run on another Mac uses a device only under a reservation held here, which it carries:

```sh
qk remote admit --device iPhone -- COMMAND
qk remote run --device iPhone LANE -- ARGUMENTS
```

- `--device KEY`, repeatable, makes the dispatch a bounded run of the reservations you hold, with the
  same checks as `qk devices run`. The bound starts when the dispatch does, so sending the commits and
  waiting for admission there count against it.
- There, `qk devices run KEY -- COMMAND` reads its reservation from `QUEUEKEEPER_DEVICE_LEASE`.
- The device stays reserved after the run; restoring and releasing it is yours, from this Mac.
- Without `--device`, a command or lane arguments that name any hardware UDID are refused. `qk run`
  keeps a lane that names one of this record's devices on this Mac, and a lane that needs a device says
  so with `"here"`.
- A 40-digit hash that names a commit rather than a device can trip the check. Declare it with
  `qk remote admit --git-commit /path/to/repository=FULL_SHA -- COMMAND FULL_SHA`, as its own argument.

The records bind only work that goes through Queuekeeper: `xcodebuild`, `devicectl` or Xcode run by
hand on another Mac reach the devices without them.

#### Notifications during a run

A notification read out by VoiceOver in the middle of a session spoils it, and the paired devices get
the owner's notifications. So reserving or taking a physical device silences them, and releasing or
recovering it puts back what was there.

**The owner's one-time setup.** On each device, the owner makes three shortcuts in Shortcuts with
exactly these names:

1. **Vocaro Silence Notifications**: Get Current Focus; If Current Focus does not have any value: Set
   Focus, turn Do Not Disturb on until turned off, then Text `eng` and Save File to the Shortcuts folder
   with Subpath `silenced.txt` (Ask Where to Save off, Overwrite If File Exists on); End If; Go to Home
   Screen.
2. **Vocaro Restore Notifications**: Get File `silenced.txt` from the Shortcuts folder (Error If Not
   Found off); Get Text from Input; If Text is `eng`: Set Focus, turn Do Not Disturb off, then Text
   `off` and Save File as above, its input the `off` Text; End If; Go to Home Screen.
3. **Vocaro Test Notification**: Show Notification "Vocaro test".

Where an action takes a value, pick the earlier action's output with Select Variable, not Shortcut
Input. The marker file means a Focus the owner already had on is left alone, and running either
shortcut twice changes nothing. The owner runs each once by hand from no Focus, answering every prompt
with Always Allow: Silence, Restore, then Restore again. In Settings, Focus, Do Not Disturb, turn off
Intelligent Breakthrough & Silencing, allow no people and no apps, and turn off Time Sensitive
notifications and repeated calls. With Share Across Devices on, Do Not Disturb follows to the owner's
other devices, Macs included, for the run. Then a session holding the device records the owner's word,
once per device:

```sh
qk devices notifications setup iPad --by owner --at 2026-11-04T09:00:00Z --words "the shortcuts are on the iPad"
```

**What a run does.**

- Reserving or taking the device runs Silence and records each step in the entry's `notifications`.
  Nothing returns the shortcut's result to the Mac, so the silencing is recorded as requested, never
  confirmed.
- Without the recorded setup, nothing runs, and the command says what to ask the owner: to turn on Do
  Not Disturb himself. Record that with `qk devices notifications by-owner KEY --owner SESSION
  --revision N --by owner --at TIME`.
- A silencing that fails, for example on a locked device, stays pending. Try again once the device is
  unlocked: `qk devices notifications silence KEY --owner SESSION --revision N`.
- `qk devices run` watches the device's log for a banner while its command runs, over USB or the
  network (`idevicesyslog`, from `brew install libimobiledevice`). A run a banner reached is unmeasured
  and runs once more; if a banner reaches the rerun too, it exits 3, so ask the owner to check Do Not
  Disturb. A run whose log could not be read is recorded as not watched and keeps its status: read its
  transcript for a notification. Only the count of banners is kept, never what one said.
- The iPhone's log reaches the Mac over Wi-Fi; the iPad's does not, so plug the iPad into this Mac for a
  watched run. Runs sent with `qk remote --device` are watched from this Mac the same way.
- `qk devices release` runs Restore first and records whether it worked; a failed restore is reported
  for the owner and the release goes on. `qk devices notifications restore KEY --owner SESSION
  --revision N` puts them back without releasing.

**An overdue silencing.** A held device past its release time that still has a restore pending may keep
Do Not Disturb on the owner's devices. `qk doctor` warns of it and `qk devices status` prints an
`Overdue silencing:` line naming the holder, the restore command and when Queuekeeper will restore it.
Fifteen minutes past the release time, Queuekeeper runs Vocaro Restore Notifications itself, records it
in the entry (`silence: put back`, with `putBack` saying when, why and how the owner was told) and tells
the owner with a banner on this Mac. The reservation stays held, so nothing is taken from the session;
a run on it afterward says the notifications are not silenced, and `qk devices notifications silence
KEY` silences them again. A restore that fails stays pending, is tried again every ten minutes, and the
owner is told of the first failure. `doctor`, `status`, a waiting `take` and the upkeep that the commands
reaching the hosts start each do this when it is due. `QUEUEKEEPER_DEVICE_RESTORE_GRACE` sets the grace
in seconds for a check that cannot wait fifteen minutes.

#### The device pass

A project's physical-device checks run as one release lane whose command is `qk device-pass`. It runs
the command once on each device named, in turn:

```json
"device-voiceover": {
  "command": ["python3", "scripts/qk", "device-pass", "--device", "iPad", "--device", "iPhone",
              "--", "python3", "scripts/run-device-voiceover.py", "--device", "{udid}"],
  "resource": "heavy", "here": "physical devices, reserved in this Mac's device record",
  "timeout": 7200, "minimumTests": 2
}
```

- **It runs here.** The lane is heavy and says `"here"`, since devices are reserved only in this Mac's
  record. `qk doctor` warns of a device-pass lane without `"here"`, and of a `gate` that runs one.
- **Devices in turn.** For each device, in order, the pass uses your own reservation if you hold one and
  leaves it held; otherwise it takes the device in turn, silencing notifications, and releases it
  afterward. A device whose turn does not come within `--wait` is not run. The pass checks no approval,
  so run a lane that names a device without standing approval only once the owner has agreed. Name
  first a device whose window is shorter than the others' wait.
- **Each run is a bounded run**, as with `qk devices run`, watched for banners and rerun after one.
  `{udid}` and `{key}` become the device's UDID and entry key, which the command also finds in
  `QUEUEKEEPER_DEVICE_UDID` and `QUEUEKEEPER_DEVICE_KEY`. `--launched BUNDLE` and `--apps-quit` say what
  the command launches and how it quits it. A script that builds first can call `device_pass.run(KEYS,
  COMMAND)` from Python.
- **Admission waits only as long as the window allows.** A lane on a device you already hold waits until
  the latest start that leaves room for its run and the reservation's cleanup; past it, it returns busy.
  Set `deviceRunMinutes` to the lane's whole time (build, every device, cleanup). Queuekeeper uses the
  larger of that and the median of its last five passing runs. A lane whose script calls
  `device_pass.run` names its devices in `"devices": ["iPhone"]`.
- **Verdict.** The pass exits 0 only when every device passed. A failure on any device fails it; a
  device not run, a run its bound stopped or one a banner reached twice leaves it unmeasured. Each
  device's outcome is in the lane's `device-pass.json`. A device it took and could not release stays
  held, with its release command, and the pass exits 2.

#### A stuck CoreDeviceService

CoreDeviceService on a Mac can get stuck while `devicectl list devices` still lists its devices: every
command to one then hangs until its timeout. So before each device's run in a pass, and before each
notification shortcut, Queuekeeper asks `xcrun devicectl --timeout 30 device info details --device UDID`,
which a healthy service answers in well under a second.

- No answer within about 45 seconds, for a listed device, is a stuck service: that device's part is
  unmeasured, saying so.
- A refusal, such as a device not unlocked recently, is reported as the device's, in `devicectl`'s words.

The fix is to restart the service, once no other device run is in progress on that Mac: quit Device Hub
and run `pkill -x CoreDeviceService`; launchd starts a new one at the next request. It breaks every
device session on the Mac, so Queuekeeper names it and never runs it.

#### Declared devices

A device belongs to the farm, not to a Mac: one on Wi-Fi is reachable from every Mac, one on a cable from
one. The owner declares what should be true of each device, and Queuekeeper reports what each Mac
actually reaches. Declarations only report: they place no lane and change no entry.

```sh
qk devices declare iPad --by owner --reach network --absent error
qk devices declare iPad --by owner --reach cable,network --cable-on studio --absent error
qk devices declare iPhone --by owner --reach cable,network --absent offline
qk devices declare iPhone --by owner --clear
```

- **`--reach`** lists the ways a Mac may reach it, in order of preference: `cable`, `network` or both.
  A way not listed counts for nothing. `--cable-on` names the Mac its cable is expected on.
- **`--absent`** says what it means when no Mac reaches it: `error` for a device that should always be
  there, `offline` for one that may be away.
- A device is declared under its entry's key, once its first reservation has recorded its UDID, and only
  in the record that holds physical devices.

`qk devices status` lists each device and declaration with what each Mac reads now: reached by a way,
by a way not declared, or not listed. A device is **unreachable** once no Mac has reached it by a
declared way for 300 seconds, counted from the first reading that missed it. Then a device declared
`error` is an error in `qk doctor`, in `qk queue health`'s Devices sign and in `status`; one declared
`offline` is reported as offline, with no error or alert. If a Mac could not be read, the device is
"not known" rather than unreachable.

### The simulator pool

Scripts that pick a simulator by name, or use `booted`, end up on Xcode's default simulators or on one
another agent is using. A project's pool gives each run its own: simulators named `POOL LABEL N`, such
as `Pagepour iPhone 17 Pro 2`, where `simulatorPool` in `queuekeeper.json` names the pool. A member's
*kind* is its device type and runtime. Each Mac has its own pool and record, so a lane on another Mac
takes members there.

`qk simulator-tests` takes one member per device type, builds the tests once while they boot, and runs
them side by side. A project's own runner takes members itself:

```sh
udid=$(qk admit --here -- qk simulator-pool take --device iPhone-17-Pro --runtime iOS-27-2)
qk simulator-pool extend UDID [--hold MINUTES]
qk simulator-pool release UDID
qk simulator-pool release --mine     # every member you hold whose take has ended
qk simulator-pool check [--held] UDID
qk simulator-pool list
```

- **A take** reserves a shut-down member of the kind, or creates the next one, resets it, boots it and
  prints its UDID. It exits 3 when the boot reaches its limit, and the run is unmeasured, not failed. A
  member that fails to boot is passed over for another, twice by default (`--boot-attempts N`). Booting
  is heavy work, so outside an admitted lane take a member under `qk admit --here`, as above.
- **A member is held for an hour** (`--hold MINUTES`). Extend it if you need longer; once its time has
  passed and its session has gone, it may be reaped. Session closeout runs `release --mine`.
- **`check UDID`** exits 1 for anything but a pool member, so a script that takes a UDID from its caller
  fails loudly rather than filling a default simulator with apps. `check --held` also requires that you
  hold it. `qk doctor` and `qk disk --delete` delete Xcode's default simulators when they are shut down
  and unused.
- **Before releasing**, copy evidence off: the next session resets the member.
- Keep the names of a configured pool's members and leave their deletion to pruning.

**Reset, keep or erase.** By default a take resets the member from its kind's *warmed template*, a
simulator that has finished its first boot and first used VoiceOver and the system voices, so the member
and `xcodebuild`'s clones of it start in seconds rather than minutes. Resetting keeps the member's UDID,
and with it the simulator tool's approval.

- `--keep-state` (`qk simulator-tests --keep-state`) boots the member as its last holder left it: apps,
  data and settings included. Use it only for tests that depend on none of that, and still set what you
  rely on, such as appearance or volume.
- `--erase` gives pristine data instead of the template's. A take also falls back to erasing when no
  settled template can be copied, and says why.
- `qk simulator-pool template --device TYPE --runtime RUNTIME [--refresh]` warms a kind's template with
  generous limits. Run it before lanes on a new runtime, or when a take reports a cold or unsettled
  template. A take makes a missing template itself, within shorter limits.

**App accessibility.** A hosted test that reads UIKit's and SwiftUI's own accessibility in process, such
as an accessibility snapshot of system views, asks for it with `--app-accessibility` on
`qk simulator-tests` or `qk simulator-pool take`. Queuekeeper sets it after each boot and fails the
device if it does not read back. It does nothing for a hostless test bundle.

**Cleanup.** `qk simulator-pool reap [--dry-run] [--here]` shuts down a member or template left booted
for two hours that nothing accounts for; it also runs before work goes to another Mac and in
`qk doctor`. A member unused for an hour may be deleted, except each kind's lowest-numbered one, which
goes only when you ask (`qk simulator-pool prune` or `qk disk`) and every member of the kind has been
unused for 7 days. `qk simulator-pool list` shows each member and template and whether it may be pruned.

**Renaming a pool.** Changing `simulatorPool` in `queuekeeper.json` leaves the old pool's members and
warmed templates on each Mac. The project owner can remove them once the rename has landed, no
project or script still selects the old pool, and all runs that used it have finished.

1. On each Mac that hosted the old pool, run `qk simulator-pool list --pool "OLD POOL"`. Identify the
   old pool's numbered members and its `OLD POOL LABEL template (RUNTIME)` templates by full name
   and UDID. Check ownership carefully if other pools share the same name prefix.
2. Check each device is `Shutdown`. Read that Mac's device record (`qk devices path` gives its path)
   and check every entry keyed by the simulator's full name or naming its UDID has been released.
   If a simulator is still held, have its holder finish and release it before continuing.
3. The owner runs `xcrun simctl delete UDID` for each confirmed device on its Mac. Run the old
   pool's list command again on each Mac and confirm that none of the old members or templates remain.

**Stale explicit modules.** Derived data reused across a dependency change can fail with `has been
modified since the module file`. `qk simulator-tests`, and any Apple lane's `xcodebuild` or `swift`
called by name, removes the stale module cache inside the checkout and runs once more, saying `STALE
MODULES`. It never runs a third time. A script that calls `/usr/bin/xcodebuild` by path, through `xcrun`,
or with PATH cleared bypasses this.

**A replaced Metal Toolchain mount.** macOS mounts the Metal Toolchain at a path with a random suffix,
`/var/run/com.apple.security.cryptexd/mnt/com.apple.MobileAsset.MetalToolchain-VERSION.SUFFIX/`, and can
mount it again at a new one. A build folder reused from before keeps the old path in its build plan, and
compiling a `.metal` file fails with `unable to spawn process '.../metal' (No such file or directory)`
though `xcrun -f metal` finds it. The same wrapper removes each `Intermediates.noindex/XCBuildData` inside
the checkout that names the old mount and runs once more, saying `STALE TOOLCHAIN`, and records it in the
lane's `results/stale-toolchain.log`. A build that fails so again, or whose plan is outside the checkout,
such as in `~/Library/Developer/Xcode/DerivedData`, leaves the lane unmeasured, not failed: nobody's entry
is sent back, and the hold quotes what to remove ([No verdict is not a failure](#no-verdict-is-not-a-failure)).

### Foreground runs

A lane that needs a Mac's screen, keyboard, pointer, focus or speech, such as Mac VoiceOver or Mac
XCUITest, is marked `"foreground": true` with a heavy or exclusive resource. `qk run LANE` sends it to
another Mac whose settings take foreground work (`qk farm foreground NAME`), never to this one, and stops
with what the owner must change if that Mac is not ready.

```sh
qk remote foreground                     # 0 ready; 78 with what the owner must change, and who holds it
qk remote run LANE -- KEY=VALUE ...
qk remote admit --foreground -- COMMAND  # one command, under that Mac's foreground and admission
qk admit --foreground -- COMMAND         # on that Mac itself
```

`--exclusive` with `admit --foreground` holds the whole Mac for a timing measurement. `--app NAME` names
the app under test, as a lane's `"apps"` does.

#### Setting up a Mac for foreground work

These are the owner's settings, made once; Queuekeeper changes none of them. Every foreground run checks
them first and stops with exit 78 while one is missing.

1. **The account is logged in on the Mac's own screen**, with no other account switched in.
2. **The screen stays unlocked.** In Lock Screen settings, the screen saver and display sleep are never,
   with no password after either.
3. **Accessibility for commands over SSH.** In Privacy & Security, Accessibility, add and turn on
   `/usr/libexec/sshd-keygen-wrapper`, and System Events (`/System/Library/CoreServices/System Events.app`)
   too, since it posts the keys a run sends. `qk remote foreground` does not yet check System Events'
   grant.
4. **Screen recording for commands over SSH.** Add `sshd-keygen-wrapper` to the Screen Recording list.
   macOS also asks separately, per app path, to let an app "bypass the system private window picker",
   and asks again 30 days after each approval. So launch capturing runners from one stable path per Mac,
   under `~/Library/Application Support/queuekeeper/foreground-runners`, and have the owner allow it
   again about once a month; `qk remote foreground` lists when each approval lapses. A run that meets
   the prompt is unmeasured.
5. **XCTest automation without a password:**
   `sudo automationmodetool enable-automationmode-without-authentication`.
6. **No VoiceOver welcome dialog:**
   `defaults write com.apple.VoiceOverTraining doNotShowSplashScreen -bool true`.
7. **No Dictation offer:** `defaults write com.apple.HIToolbox AppleDictationAutoEnable -int 0`.

XCTest's "Automation Running" banner cannot be turned off with System Integrity Protection on. A session
VoiceOver stops at the banner is unmeasured, and a project's session runner runs it again a bounded
number of times. Builds signed ad hoc are unmeasured there; signing with a team needs that Mac's keychain.

#### Running and waiting

- **Results.** A lane writes what must come back, transcripts included, in the folder named by
  `QUEUEKEEPER_RESULTS_DIR`. However the run ends, it comes back to
  `.build/queuekeeper/remote/LANE/results/` here. In a lane group each lane has its own folder, so read
  the variable rather than a fixed path. `qk remote admit` brings no files back: write outside the
  checkout and copy them, or make the work a lane.
- **Arguments** after `--` reach the lane's command. Check a session's steps here first with the
  project's own check; the lane checks them again there only after waiting for the screen.
- **One run holds a Mac's foreground at a time.** A waiting run says who holds it and until when, and
  takes it as soon as it is free, for up to the 5,400 seconds `qk run` waits for admission. A holder
  expected to finish after that deadline is judged at once: the waiter looks for 180 more seconds in case
  it ends early, then exits 75 naming the command to run again.
- **The screen comes before the slot.** A run takes the foreground first and admission after it, so a
  run waiting for the screen holds no slot. Builds and background tests keep running beside a session.
- **Build before the screen.** A lane holds the foreground for its whole command, so a slow build
  belongs in a lane of its own first.
- **When the other Mac cannot be reached**, ask the owner for a window on this Mac's foreground (see
  Attended sessions, below) rather than ending on the stop.

#### What a foreground run cleans up

- **VoiceOver** is put back exactly as the run found it, however the run ends. VoiceOver on at the start
  is a valid state, not a sign of a failed run. A restoration that cannot be verified leaves the
  foreground uncertain for recovery.
- **Apps the run opened** are asked to quit before the screen is released, and never forced. On another
  Mac that means new apps from the Applications folders, built in the run's checkout, or recorded with
  `qk devices update mac-foreground --launched BUNDLE`; on any other Mac only the last two. Anything
  else new is left running and reported in the log and the entry's `appsQuit`. Windows the run opened in
  apps already running stay.
- **Crash-report alerts** ("APP quit unexpectedly.") would take the run's clicks. For every foreground
  run Queuekeeper turns crash dialogs off and puts the exact setting back afterward, and clears any crash
  report on screen with Ignore or OK before and during the run. It never presses Reopen or Report…, and
  never touches a password or permission prompt. An alert it may not clear stops the run unmeasured
  (exit 3), naming it and where it sits. A failed run is unmeasured when a cleared alert for another
  process stood longer than one look; a crash of the app under test stays a failure. A driver can look
  between its own steps with `python3 -m queuekeeper_mac_desktop.crash_alerts check`. If crash dialogs
  are off with no run holding the screen, the owner puts them back with
  `defaults delete com.apple.CrashReporter DialogType`.
- **Notification Center windows** sit over every app. Under the foreground lease on another Mac,
  Queuekeeper closes each banner or alert with its own Close action before the run starts, never pressing
  one, clearing a stack or touching a permission request; one it cannot close stops the run unmeasured.
  It closes nothing on this Mac. `qk host notifications` lists them, exiting 0 when none is and 3 when one
  is; `qk host notifications --dismiss` closes them under a lease you hold.

#### Recovering the foreground

A run whose Queuekeeper process died leaves the `mac-foreground` entry orphaned, and every later run
stops on it, naming the recovery command. Attended recovery is the owner's decision. On that Mac, once
the run's work has stopped:

1. Put VoiceOver back as the run's `host.txt` recorded it, with Command-F5. If VoiceOver was quit
   through AppleScript, also run `defaults write com.apple.universalaccess voiceOverOnOffKey -bool false`
   and check that it reads 0.
2. Record it: `qk devices recover mac-foreground --by SESSION --revision N --verified "WHAT WAS
   CHECKED"`. From another Mac, add `--host HOST` after `recover`.

One exception needs no owner: when a session's own VoiceOver restoration could not be verified and
nothing else is uncertain, the live run's session runner may set VoiceOver back to the recorded original
once and carry on.

#### Attended sessions on this Mac

Foreground work runs on another Mac by default. A session on this Mac's screen happens only within a
window the owner agreed beforehand, under a reservation of `mac-foreground`:

```sh
qk devices reserve mac-foreground --owner SESSION --revision N \
  --purpose "Mac XCUITest of …" --release-by 2026-10-23T18:00:00Z
export QUEUEKEEPER_DEVICE_OWNER=SESSION
qk devices check      # at the start and before each action
qk devices update mac-foreground --owner SESSION --revision N --own PID
qk devices release mac-foreground --owner SESSION --revision N \
  --outcome "…" --restoration "…" --frontmost restored
```

- **`qk devices check`** refuses unless the session holds the entry, it is neither orphaned nor
  uncertain, and its release time has not passed. A driver calls it, or `devices.require_foreground`,
  before each key, activation or focus change. `qk run --here` and `qk admit --foreground` check the same
  way.
- **A passed release time releases nothing.** The check refuses further work, and the session stops,
  restores and releases.
- **A new window is a new reservation.** Never extend one yourself.
- **The screen and the CPU are separate.** A timing measurement needs the reservation and exclusive
  admission; a functional UI test needs only the reservation.
- Record each app and runner you launch with `--own PID`. The release says whether the previous
  frontmost app was restored (`restored`) or the session no longer owned the screen (`not-ours`).
- Announce each start and when the owner can work again, guard every action by the exact app path and
  PID, and restore the original settings before handing back.

### Kept app builds

`qk app` keeps the last good build of a project's app on this Mac and runs it. After the merge queue
pushes a gated batch, it starts `qk app keep --after-landing` in the background, which builds the pushed
commit with `xcodebuild build` and keeps the product. The gate's own builds are test builds in a
checkout that is reused, possibly on another Mac and never for a device, so none of them is kept. A
build that fails keeps nothing, and a docs-only or ungated landing keeps no build.

```sh
qk app run              # open the newest kept Mac build, building origin/main first if it is newer
qk app run --ipad       # install and launch the device build on the paired iPad (or --iphone)
qk app keep [--commit SHA] [--platform mac]   # or ios
qk app status           # the kept builds, and how far each is behind origin/main
```

- `qk app run` refuses while another copy of the app runs from somewhere else, such as a gate's test
  host, and names it; `--replace` quits it, which fails that gate's run. `--no-build` runs the kept build
  as it is. It uses the owner's screen, so run it only when asked.
- `--ipad` and `--iphone` run as a device pass: the device is taken in turn (or your reservation used),
  notifications silenced and put back, and the app left running. Use `--iphone` only when the owner has
  asked or agreed.

**Probe builds.** A diagnostic may need a build that never lands, such as the app with one added
logging line or a dependency pointed at a local copy. Build and run it from the worktree that holds
the change:

```sh
qk app build [--platform ios]      # build this working tree as it stands; prints the .app's path
qk app run --ipad --app PATH       # install and launch that .app (or --iphone)
qk app run --ipad --here           # both in one command
```

- `qk app build` runs the project's `prepare` steps and the platform's `settings`, such as
  `DEVELOPMENT_TEAM`, in the environment `qk` gives its lanes (folders such as
  `APPLE_ENGINEERING_ROOT` included). The build runs through `qk admit --here` into a build folder of
  that checkout's own in the store, so it never touches the kept builds, and nothing is kept.
- `--app` takes any `.app` built for the platform: for `--ipad` or `--iphone`, a device build (for
  `generic/platform=iOS`), not a Simulator or Mac build. Its bundle identifier must be one the project's
  kept or probe builds have; otherwise it is refused, with the reason.
- A probe build runs under the same device pass as a kept build: the take, the silenced
  notifications, the launched-app record and the release.
- `qk disk` lists each checkout's probe builds as a cache, deletable once idle.

Configure it with `app` in `queuekeeper.json`:

```json
"app": {
  "project": "Name.xcodeproj",
  "configuration": "Debug",
  "prepare": ["xcodegen", "generate", "--quiet"],
  "mac": {"scheme": "Name"},
  "ios": {"scheme": "Name", "settings": {"DEVELOPMENT_TEAM": "TEAMID"}}
}
```

- `project` or `workspace` (one of them) is a path inside the checkout; `directory` is where the build
  runs, by default the checkout's top. `prepare` is a command run there before each build, as an argument
  list, or a list of argument lists run in order, such as
  `[["xcodegen", "generate"], ["pod", "install"]]`; the first to fail stops the rest and the build. Each
  command's output goes to its own log beside the build's.
- `mac` and `ios` each name the `scheme`, and optionally the `product` (when the build makes more than
  one app) and `settings` for xcodebuild's command line. `configuration` defaults to Debug.
- `afterLanding` on each platform says whether the queue keeps it after each landing: by default the Mac
  build yes, the device build no (it is built when a device asks). Each keep is one admitted build on
  this Mac.
- **Signing.** The device build uses `-allowProvisioningUpdates`, which needs the signing team, an Apple
  Account signed in to Xcode on that Mac, and every target, extensions included, signing automatically.
  XcodeGen writes no `CODE_SIGN_STYLE`, so set `CODE_SIGN_STYLE: Automatic` in the spec; choosing the team
  in Xcode does not survive `prepare`.

Everything lives under `~/Library/Application Support/queuekeeper/apps/NAME/`: a locked `source/`
worktree, each platform's DerivedData in `derived/` (kept warm, listed by `qk disk` once idle), the
newest two builds per platform in `kept/`, and each build's log in `logs/`.

### Unity editors

A project that lists the Unity plugin runs its editor unattended. Unity's lock, `Library/`, `Temp/` and
its Pipeline server are all per project path, and each Git worktree is a separate path, so each worktree
may have its own editor open. The cost is a cold `Library/`: a new worktree's first editor imports every
asset and compiles every shader. Reuse worktrees, and let `qk disk` reclaim idle ones.

What sessions on one Mac share:

- **The GPU.** A time measured while another run uses it is void, so run measurements in an exclusive
  lane.
- **An editor in Play mode**, which keeps simulating unfocused and which admission cannot see. An
  exclusive `"unity": true` lane ends unmeasured while one plays on that Mac.
- **The licensing helper**, which the plugin stops when no editor is running before it starts one.
- **Pushes.** A pre-push hook that runs the tests starts a batch editor, so ask a session that is
  measuring to hold its pushes too.

A batch run needs the project lock, which an open editor holds:

```sh
qk unity closed -- ./tools/verification/check.sh --gates
```

closes the project's editor, runs the command, and opens the editor again. It never force-kills; an
editor that will not close, say for unsaved changes, ends the run unmeasured. A lane that runs the editor
says `"unity": true`, and Queuekeeper checks on the Mac that runs it that a batch run can start; when it
cannot, the lane ends unmeasured rather than failed.

A second session on a Mac:

1. Create the worktree, and open its editor with `qk unity open --ensure` when needed, expecting a cold
   import the first time.
2. Run `qk host editors` before driving any editor, to see which are open and which is in Play mode.
3. Keep at most one editor in Play mode, and run measurements alone, in an exclusive lane.

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
| 70 | Queuekeeper itself could not start (a plugin did not load), so nothing ran |
| 75 | Busy, held, or the wait reached its limit; from `qk queue status --idle`, the queue is not idle |
| 78 | A Mac is misconfigured |
| 127 | A lane's command could not start |

### Output

- **Every run ends with fixed status lines**: the verdict, test counts, up to twenty failing
  tests with file and line, and the path of the full log. A run that failed without naming a test
  also quotes its command's last lines.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `land`, `queue add`, `queue wait`, `queue result`, `queue status`,
  `queue inbox`, `queue confirm`, `queue logs`, `queue doctor`, `queue health`, `queue placement`, `queue events`,
  `admissions now` and `doctor`.
- **`qk queue status --idle`** answers by exit status: 0 only when nothing is queued, testing or
  passed and no worker runs, or the one that runs only waits to move the shared checkout, and 75
  otherwise.

An agent that cannot keep a command running in the background can use the
[MCP server](../plugins/mcp/src/queuekeeper_mcp/README.md) instead, which offers `land`,
`queue wait`, `queue result`, `queue status` and `queue logs` as tools, with progress while it
waits.

### Following a landing

`qk land --wait` follows a commit to its result, so start it in the background and let its end
wake you.

- **While it waits**, it prints a line each time the entry's situation changes: waiting behind a
  round, gating on a Mac, a failed gate's tests rerunning alone (each rerun, its log and what it
  waits for), halved in a bisect, passed and waiting for the push. When nothing changes, a
  `heartbeat:` line comes at most every five minutes. With `--json`, these lines go to stderr.
- **A send-back ends the wait within seconds**, naming the conflicting files and entries, or the
  failed tests and the log.
- **A gate running past its usual time** shows `timing: warning: ...` in these lines. A lane stopped
  by a limit prints a `queuekeeper notice:`. Do not raise a `timeout`, stall bound or timing threshold
  just to make the gate pass; find what made it slower, or after a deliberate change to the lane's
  work run `qk timing stale LANE --reason WHY` ([Time limits](#time-limits-and-the-gates-budget)).
- **A repository with no `main` yet** takes its first landing the same way, and that landing
  creates `main`. A commit the queue cannot use for it is refused with exit 1, saying why
  ([A project's first landing](#a-projects-first-landing)).
- **An interrupted wait withdraws nothing.** The worker lands the entry whether or not anyone is
  waiting. Pick up the result with `qk queue wait ID`, which behaves like `qk land --wait`, or
  find it in `qk queue status`. Queue the commits again only after confirming the entry came back
  or was never queued.
- **Running `qk land BRANCH --wait` again** while the branch's entry is still queued adds nothing:
  it prints `ID already holds TIP (STATE)` and follows that entry. A branch with new commits
  replaces its earlier entry instead.
- **A refused entry because the queue is closed** gets `state: refused` and
  `reasonCode: "queue-closed"` in `--json`. Wait for the queue to reopen, then land again.
- **A branch behind `main`'s configuration** is refused with exit 1 when `origin/main`'s
  `queuekeeper.json` changed after the branch left `main`, and the branch's Queuekeeper can't read
  the new file, for example because it uses a key added since. `--json` gives
  `reasonCode: "behind-main"`, `action: "rebase"`, `retryable: true` and the suggested command
  `git rebase origin/main`. Rebase, then land again
  ([When main's configuration can't be read](#when-mains-configuration-cant-be-read)).
- **A branch built on a rewritten `main`'s old history** is refused with exit 1. `--json` gives
  `reasonCode: "rewritten-main"`, `responsibleParty: "session"`, `action: "rebase"`,
  `retryable: true` and the suggested `git rebase --onto origin/main COPY BRANCH`. Run it,
  cherry-pick any of your commits the refusal lists, then land again
  ([Checks before queuing](#checks-before-queuing)).
- **A landing deletes its branch** when it can prove the branch landed, and says
  `queue: deleted branch NAME at TIP: ...; restore with git branch NAME TIP`, or
  `queue: kept branch NAME: WHY`. The usual reason to keep it is that your worktree has it
  checked out. `qk land --wait --detach-landed` deletes it anyway once it is proven landed, by
  detaching your worktree at the same commit, which changes no file. Otherwise the next worker to
  start deletes it once no worktree has it checked out. `--json` gives the same as
  `branchCleanup` ([Cleaning up after a landing](#cleaning-up-after-a-landing)).
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

An entry's result names its `commit`, `landedAs`, failure details, the entries it fails together
with (`conflictWith`, empty for others), test counts, flaky tests (`flakes`), timing warnings and limit notices
from its logs (`timing`) and the run's artifacts. A missing or pruned artifact is listed with
`available: false`. Results carry `schemaVersion: 1`; tolerate added keys and unknown states.

Every result has a `recovery` object that says what to do next:

| Key | Meaning |
|---|---|
| `reasonCode` | A category such as `queue-held`, `wait-limit`, `entry-failed` or `publication-unverified` |
| `responsibleParty` | `owner`, `session`, `unknown`, or null when no action is needed |
| `retryable` | Whether the same operation can simply be repeated |
| `action`, `message`, `suggestedCommands` | The next step, with commands as argument lists |

A held queue suggests a diagnosis, never a blind resume. An expired wait leaves the entry queued
and suggests following it again.

A waiting entry whose push got no answer has `reasonCode: "publication-unverified"`, and its
`publication` object has `status: "unverified"` and the pushed `heads`. (`status: "pushing"`
means a push is in flight right now.) The worker settles the entry by fetching `origin/main`, so
follow it with `qk queue wait ID --json` and never submit it again. If the queue is held as well,
the advice is the hold's, since the next round can't check until the hold's cause is fixed.

### Claude Code's hooks

`qk agents setup` installs these in `.claude/settings.json`. Each reads the event's JSON on
standard input, answers in Claude Code's hook format, and exits 0 whatever goes wrong, so a hook
never stops a session.

**`qk hook session-start`** tells a starting or resumed session what became of the entries its
worktree or branch queued earlier. Of each branch's entries it reports the newest when it is
still waiting, with the hold when the queue is held, or came back in the last fourteen days and
is still in the queue's records. Each line gives the reason and the entry's
`recovery.suggestedCommands`. An entry queued from the same folder on another branch belongs to an
earlier session that used the folder, so it is left out unless the checkout is on no branch, as in
a rebase. It says nothing otherwise, and never creates a queue.
`qk queue remove ID` silences an entry that came back and is no longer wanted. The hook is:

```json
{"SessionStart": [{"hooks": [{"type": "command", "command": "qk hook session-start", "timeout": 15}]}]}
```

**`qk hook pre-tool-use`**, with the matcher `Bash`, refuses a command that would land on `main`
by hand and points the agent to `qk land --wait`:

- `git push` to `main`: a refspec whose destination is `main`, `--all` or `--mirror`, or a bare
  push from `main` or from a branch whose push destination is `main`;
- `git merge` on `main`, except `--abort`, `--continue` and `--quit`;
- moving `main` with `git update-ref` or `git branch -f`;
- `gh pr merge`.

It reads each command of a line, follows `cd`, `git -C` and `sh -c`, and skips redirections,
variable assignments and `sudo`. It is a guard rail against a mistake, not a security boundary,
and lets through what it cannot parse. Set `QUEUEKEEPER_GUARD=off` in Claude Code's environment
to turn it off for a session.

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

### Next actions and the outcome inbox

`qk queue result --json` and `qk queue status --json` also carry `nextAction`: the single command
to run next, as `{"command": [...], "message": "..."}`, or null when nothing is left to do. For an
entry still waiting it follows the entry, or diagnoses the hold. One sent back reads its log
(`failed`) or rebases onto `origin/main` (`conflict`), and the message says to fix, commit and land
again. One landed or removed is confirmed, which takes it out of the inbox. Status's `nextAction`
is for the session asking: a hold comes first, then this worktree's or branch's unconfirmed
outcomes, then its entry still waiting.

`qk queue remove ID --why TEXT` records the reason as the entry's `removedWhy`, which
`queue result`, `queue wait`, `queue inbox` and the `recovery` message say. Give one whenever you
remove another session's entry.

#### The outcome inbox

A session that died, or lost its entry ID, recovers with `qk queue inbox --json`. It lists the
entries of this worktree or branch whose outcome nobody has confirmed: `landed`, `sent-back`
(failed or conflict), `removed`, and `held` for an entry waiting in a held queue. Only the newest
entry of a branch counts, since landing a branch again replaces its earlier entries, and a settled
outcome leaves the inbox after 14 days.

```json
{"schemaVersion": 1, "operation": "queue.inbox", "branch": "fix-parser",
 "nextAction": {"command": ["qk", "queue", "logs", "qk1093-fix-parser", "--json"], "message": "Read the failing run's log, ..."},
 "entries": [{"id": "qk1093-fix-parser", "state": "failed", "outcome": "sent-back",
              "subject": "Fix the parser", "branch": "fix-parser", "settled": 1795000000.0, "landedAs": null,
              "reason": "The gate failed", "recovery": {"reasonCode": "entry-failed"},
              "nextAction": {"command": ["qk", "queue", "logs", "qk1093-fix-parser", "--json"], "message": "Read the failing run's log, ..."}}]}
```

Both `nextAction` and `recovery` are abbreviated here; the real command also names `--project`.

`qk queue confirm ID` acknowledges one outcome and exits 0, or 1 for an entry still in the queue.
Confirm a landing or removal once you have acted on it; fix a send-back and land again, which
replaces it. Removing your own entry confirms it, since your session already knows.

### Verifying

Every verification run goes through admission, so it takes a slot and shows in
`qk admissions now`:

- `qk unittest -s tests NAME...`, where NAME is a module, class or test;
- a lane, `qk run LANE -- FILTER`;
- any other command, `qk admit -- COMMAND`.

Never run `python3 -m unittest` directly, however small the run. It takes no slot, so other work
waits behind load nobody can see, and it misses `qk unittest`'s summary, logs, progress and the
suite's own setup. Only a test of a runner itself uses `--unadmitted`
([Admission](#admission)).

`qk run --expect N LANE -- FILTER` checks that exactly N tests *executed*. Skipped tests don't
count: if executed plus skipped make N, the run is unmeasured (exit 3), and any other count fails
(exit 2). The message says which N would count what ran.

### Long runs

Keep a long run's progress readable while it runs:

- **Write its output to a file**, `COMMAND > FILE 2>&1`, as a background command, and trim when
  reading. Never pipe a long run through `tail` or `head`, which print nothing until the end.
- **Give each run a file of its own.** Sessions or subagents that share a scratch folder must not
  share a file name in it: a `qk land --wait` that writes `land.log` beside another's can find that
  one's record and `exit 0` there first. A `--json` record names its entry in `id`; check it is yours.
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
| Trends | The last 7 or 30 days: landings per day, how often entries came back, gate times and flaky tests; each lane's [timing baseline](#time-limits-and-the-gates-budget) on this Mac, its p90 and any fail threshold drawn over its recent passing runs, with where its history was marked stale, or how many more passing runs it needs |
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

When the queue stops, it *holds* with every entry still waiting, and the hold's reason begins by
saying whom it is for:

- **`for a session, not the owner`**: most holds. Any session that fixes the cause, or finds it
  gone, runs `qk queue resume` and says so in its report, without asking the owner. A cause a
  session cannot find is still a session's: say what was looked at.
- **`for the owner, since nobody else can fix it`**: the remote denied repository access,
  for example because it refused its key or token. Once the owner has fixed it, any session
  resumes. With the [notify feature](#the-notify-feature) on, you hear about these at once.

`qk queue status`, `qk land` and `qk land --wait` print the reason. A batch that passed before a
hold stops its push is kept; after `qk queue resume` it is pushed without gating again if nothing
changed.

Common holds:

- **Queuekeeper itself could not start** in a lane (exit 70). No commit was tested. Bring
  Queuekeeper and the folders the project's plugins load from to versions that work together.
  `qk queue resume` starts a worker from the current checkout, which is often the whole repair.
- **A lane's command could not start** (exit 127). Make the command start, or remove the entry
  that took away what it runs.
- **Out of disk space.** Free space with `qk disk --delete` and resume.
- **A refusal `qk unittest` gives every gate** with no entry to blame, such as a broken module
  that reached `main` past the queue. The reason quotes the line.
- **A plugin folder beside the project.** A run loads such a folder at its `origin/main`, not
  as your checkout stands. Test a change to it with `qk run --sibling FOLDER=WHERE LANE`, and land
  it before the change that needs it.

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
reports on each Mac, without changing its verdict. Run `qk doctor --check-only` to report setup
findings without repairing Git hook configuration, device records or the farm's toolkits and simulators.
Publication and gate send-backs that include diagnostics keep this report as `setup-doctor-log` and the
queue report as `queue-doctor-log` in `qk queue result --json`. Read either with `qk queue logs ID --artifact KIND`.

### Disk space

Run `qk disk` to see what Queuekeeper may delete, and `qk disk --delete` to delete it. A lane that
waits on free space or stops with `queuekeeper: out of disk space:` is covered by
[Free disk space](#free-disk-space). The whole story is in
[Disk space and leftovers](#disk-space-and-leftovers).

## Disk space and leftovers

Queuekeeper keeps a Mac's disk and process table clean: `qk disk` deletes only what is provably
unused, two free-space floors protect a run, and leftover processes, pool simulators and Mac app
registrations are found and, where it is provably safe, cleaned up.

### Taking the inventory

`qk disk` takes the inventory and changes nothing:

```sh
python3 scripts/qk disk              # everything, with its status and why
python3 scripts/qk disk --dry-run    # only what --delete would remove
python3 scripts/qk disk --json
```

- **What it reads.** Every project in `~/Development` (`--development` names another folder), and
  only what Queuekeeper and its plugins create: simulators and runtimes, DerivedData and SwiftPM
  caches, lane results kept in each Git directory, kept app builds, the checkouts runs were
  pinned to and Unity projects' generated folders. With the Apple plugin it also lists Xcode's
  device support folders and a few large caches of macOS tools in `~/Library/Caches`. Never the
  rest of the home folder or the volume.
- **What it prints.** Each item's size, why it has its status, and the exact command that removes
  it. `--min-size-mb` lists small items only as totals.
- **Progress.** Stderr names each pass and removal as it starts. Stdout is written line by line,
  so output saved to a file shows how far it got.
- **What sessions leave** (linked worktrees, scratch builds in `/private/tmp`, evidence) is the
  engineering toolkit's `eng disk` to remove. When short of space run both, then `qk disk` again,
  since removed worktrees make their DerivedData a candidate.

### What may be deleted

Each item is one of three kinds:

- **Candidate**: proven unused by the rules below. `--delete` removes it without asking.
- **Owner item**: shows no sign of use, but only the owner can say whether it is still wanted,
  such as test results, a folder no repository registered, or a worktree outside the session
  folders. Its command is printed, never run.
- **Kept**: in use, owned, of unclear ownership, or holding work or evidence.

A path is **in use**, and kept, when any of the account's processes has its working directory, an
open file or an argument inside it. The rules for each category:

| Category | A candidate when |
|---|---|
| Pool simulators | The pool's pruning rule allows it; removed by `qk simulator-pool prune` |
| Old disposable simulators (UUID in the name) | Shut down, idle, and named by no process and no device record |
| Xcode's default devices, unavailable devices, orphaned device folders | Always (`qk doctor` deletes this Mac's defaults itself). Any other named device is kept, since it may belong to a pool Queuekeeper cannot see |
| Simulator runtimes | No device uses it and it was last used over 14 days ago, or it is a superseded build of an identifier last used over 14 days ago. Kept if it came with Xcode, is pinned by the toolkit or named by a device record (such as a pool template's `runtimeBuild`), or another Mac still uses its copy |
| DerivedData | Its `info.plist` names a workspace that no longer exists; or it has no `info.plist`, no process, has been idle for `--min-idle-hours` (24), and holds only what a stray `xcodebuild` leaves (`Logs`, `SourcePackages`, `TestResults`, or a `Build` with only `ProfileData`). Shared folders such as `ModuleCache.noindex` are kept |
| Xcode device support (`~/Library/Developer/Xcode/iOS DeviceSupport`, and watchOS, tvOS and visionOS) | Idle and unused, and for a beta build (its build number ends in a letter, such as `24B5110a`), or the same platform has a later release build for the same device model. A model's newest release build is an owner item. Xcode copies a folder again when a device on that build connects |
| SwiftPM's cache | Its clones and artifacts, which SwiftPM fetches again |
| Caches of macOS tools (`~/Library/Caches`) | Idle and unused: `com.apple.python` (the system Python's bytecode) and `com.microsoft.VSCode.ShipIt` (VS Code's past update downloads). `Homebrew` (current downloads `brew cleanup` keeps) is an owner item. No other cache there is listed |
| Unity projects' `Library`, `Temp` and `Logs` | Idle, no editor has the project open, and Git ignores the folder and tracks nothing in it. Otherwise kept |
| Kept lane results | Beyond each lane's newest 10 commits and older than 14 days; removed by `qk remote prune-results CHECKOUT`. Receipts are never removed |
| Pinned checkouts | Unused by any run for a week and by any process; removed by `qk disk --prune-pins` |

**No runtime is removed while any simulator on this Mac runs or is reserved**, on any runtime: they
share one launch host, and deleting a runtime can crash it. An unreadable device record keeps every
device and superseded runtime it might name.

### Deleting

```sh
python3 scripts/qk disk --delete [--yes 'WHO ASKED']
```

- **Any session may run it** when the volume runs short. It removes every candidate without
  asking; `--yes` only records who asked.
- **Each removal is checked again** just before it, with a fresh census. It never removes through a
  link, nor inside a working tree, except a folder a tool generates there, such as a Unity project's
  `Library`, while Git ignores it and tracks nothing in it and no other repository is nested
  around it. It logs every decision to
  `~/Library/Application Support/queuekeeper/disk-reclaimed.jsonl`.
- **Report the space freed by measuring it.** Sizes are allocated sizes, and APFS clones share
  blocks, so a removal can free less than its size. Compare `df -k /System/Volumes/Data` before and
  after, and call the dry run's total "allocated size".

### Free disk space

A run that fills the disk is lost, and can hang while holding the queue and the Mac. Two floors, in
GiB free on the data volume and on the checkout's volume, prevent it:

| Variable | Default | Effect |
|---|---|---|
| `QUEUEKEEPER_START_FREE_GB` | 10 | A whole-host or exclusive lane, or one with `compileFirst`, waits for this much before asking for admission, holding no slot. `qk queue status` shows `waiting: free disk space: ...`, with the floor and where it comes from. Waiting longer than the lane's admission wait exits 75 and runs nothing. A project can set its own floor (below) |
| `QUEUEKEEPER_RUN_FREE_GB` | 2 | A running admitted command is checked every 10 seconds. Below the floor, or when a write finds the disk full, it is stopped as a timeout would stop it: unmeasured, exit 3, with a line starting `queuekeeper: out of disk space:`. The merge queue holds at once |

Both are environment variables inherited by child processes; 0 turns a floor off. Free space with
`qk disk --delete`.

If a running lane goes quiet, its `stall` setting can report the quiet interval once it exceeds
the configured bound. The lane's latest observation appears in `qk queue status` and in the
heartbeat of `qk land --wait` or `qk queue wait`. Use the volume reading from `qk disk` to check
available space. See [Quiet lanes](#time-limits-and-the-gates-budget) for setting a report-only bound.

The start floor's default suits a lane that writes little. A project whose builds and result
bundles take tens of GiB, such as a large Xcode project, sets a higher one in `queuekeeper.json`,
and a lane or lane group can set its own:

```json queuekeeper.json
{
  "schemaVersion": 1,
  "name": "my-app",
  "plugins": [],
  "disk": {"startFreeGB": 30},
  "lanes": {
    "gate":  {"command": ["scripts/gate.sh"], "wholeHost": true},
    "timed": {"command": ["scripts/timed.sh"], "resource": "exclusive", "disk": {"startFreeGB": 12}}
  }
}
```

The floor in force is `QUEUEKEEPER_START_FREE_GB` when it is set, then the lane's
`disk.startFreeGB`, then the project's, then the default of 10. `qk doctor` reports it under
`startFree`: the project's floor and that of each lane that waits for one, each with where it
comes from.

### Processes left over from runs

Supervision can miss a child that moved to a session of its own and was orphaned between two
samples. So before work is sent to another Mac, `qk remote` and `qk run` search it and print what
they find, and `qk doctor` lists it for each Mac under `farm`, `leftovers`.

A process is a **leftover** when it is the account's, runs from a farm checkout
(`~/Development/queuekeeper-farm/checkouts`), the temporary folder or a simulator's device folder,
and no live run accounts for it. A live admission owner and its descendants account for their
processes, as do a simulator a live run reserved or names, and Xcode programs or built apps running
from the slot of a live admission that started before them. Programs under `/System` are never
listed.

Each leftover is listed with its PID, parent, start, command line, where it runs from, and the bytes
it and its descendants have written.

**The search only reports.** A failed search says why, and the work is sent anyway. Whether to stop
a listed process is the owner's decision; tell the owner what it lists, with PIDs and writes. Two
exceptions are cleaned up automatically:

- **What an ended run provably launched.** While a dispatch holds a slot, it records each process
  launchd starts from that slot's built executables in `SLOT.launched.jsonl`. Afterward, cleanup
  sends those processes SIGTERM, after checking everything again, and logs each stop in
  `leftover-stops.jsonl` beside the admission log. Anything it keeps is printed with the missing
  evidence (`did not stop PID …: …`) and left for the owner; `qk doctor` shows what it would stop.
- **Stale pool simulators**, below.

### Stale pool simulators

Any session may run this without asking:

```sh
python3 scripts/qk simulator-pool reap [--dry-run] [--here] [--pool NAME]
```

Queuekeeper also runs it before sending work to another Mac, in `qk doctor`, and once heavy work run
here by `qk run` or `qk admit` is admitted. With no options it acts on this Mac and then on another
Mac; `--here` leaves the other out, `--dry-run` says what it would do and why, and `--pool` looks
at one pool. It exits 1 when anything failed, including an unreachable Mac.

**It shuts a simulator down** (`xcrun simctl shutdown`) only when all of these hold, and still hold
at a second check just before the stop:

1. It is a member of a pool some project names, or that pool's warmed template whose creator no
   longer runs.
2. It is left over: the search above lists it, and no process outside it names its UDID.
3. No live reservation holds it.
4. It has run for 2 hours.

**A pool member with no live reservation goes after 2 hours, whoever booted it.** To keep one,
boot a simulator outside the pool, or reserve it with `qk devices reserve`. A member taken with
`qk simulator-pool take` is held for an hour (or `take --hold MINUTES`); extend it with
`qk simulator-pool extend UDID [--hold MINUTES]`.

**It is cautious.** It erases, deletes and kills nothing. It stops nothing when anything it reads
(processes, device record, admissions, simctl) is unreadable, or when its log, `simulator-reaps.jsonl`
beside the admission log, cannot be written. Before a run it spends at most 60 seconds. It takes the
device record's lock for its final check, so it cannot race a take.

**It releases dead reservations** of pool simulators, on any Mac, so nobody recovers them by hand.
It releases an entry only when every process the entry records is gone; the release is due, or
(for a run's own reservation, never a take) nothing alive names its owner or device; the entry is
certain and its run stayed on this Mac; and simctl shows the simulator shut down. Each release is
logged and printed (`released KEY (UDID), held for owner …`). An entry that is due but not provably
dead stays held, and reap, `qk doctor` and `qk devices status` name it with its exact recovery
command, which runs from any folder:

```sh
python3 ~/Development/queuekeeper/bin/qk --project ~/Development/queuekeeper devices recover …
```

For another Mac's entry the command adds `--host HOST`, which runs the recovery through that Mac's
`qk` (a plain `ssh HOST` finds a Python too old to start it).

### Cleaning up after a landing

Once an entry lands, its local branch and the worktree that held it are left behind. Queuekeeper
deletes the branch when it can prove it landed, and reports the worktree without removing it.

**The branch is deleted only when all of these hold:**

- The queue's newest entry from that branch has landed, and `origin/main`, freshly fetched, holds
  the commit it landed as.
- The branch's tip is exactly the tip that entry queued (or, for a branch refreshed by merging
  `main`, the tip the queue replayed). Otherwise the branch has no merge of its own, and every
  commit of it that `origin/main` lacks has an equal patch there (`git cherry`, which compares
  patch IDs), since the queue may rebase what it lands.
- It is not `main`, `master` or origin's default branch, and no worktree has it checked out or is
  rebasing or bisecting it.

It is deleted with `git update-ref -d` naming the tip that was checked, so a branch that moved
meanwhile stays. Each deletion is written to the queue's journal as `branch.deleted` and printed
with the command that restores it. Anything else is kept with the reason.

`qk land --wait` does this for its own branch once the entry lands. With `--json`, the result's
`branchCleanup` holds `branch`, `tip`, `entry`, `outcome` (`deleted` or `kept`), `why`, and for a
deletion `how` (`exact`, `contained` or `patch-equivalent`) and `restore`. A branch kept because a
worktree has it checked out also gives that worktree as `heldAt`. A failure here never changes
the landing's exit status. `qk land --repo` leaves the other repository's branches alone.

**A branch checked out in your own worktree** is kept, since you usually land from the worktree
that has it checked out. You can have it removed in either of two ways:

- **At landing:** `qk land --wait --detach-landed`. After the branch passes every other check above,
  it detaches the worktree you ran `qk land` in, and only that one, at the branch's tip, then
  deletes the branch. Detaching at the same commit changes no file. It does this only when that
  worktree has the branch checked out at that tip, with no change to a tracked file and no merge,
  rebase, cherry-pick, revert or bisect under way, and no other worktree holds the branch.
  Untracked and ignored files are not touched, and the worktree is never removed. The message
  and `branchCleanup` add `detached` (the worktree) and `reattach`. To undo, run `restore` and
  then `reattach` (`git -C WORKTREE switch NAME`). If Git refuses the deletion, the worktree goes
  back on the branch. Use it only when you don't need that branch checked out after landing;
  some clients track the session's branch by name.
- **Later:** each worker, when it starts, deletes every branch the queue proves landed that no
  worktree has checked out, using `origin/main` as last fetched. Each deletion is printed in the
  queue's log and journaled as `branch.deleted`. A branch whose worktree has since been removed,
  or switched away from it, therefore goes with the next `qk land` without anyone running
  `qk queue reclaim`.

**Repository-wide**, `qk queue reclaim` deletes every branch that meets the rules above and
reports each linked worktree that holds only landed work: its branch passes the same checks, or it
is detached at a commit `origin/main` holds. A reported worktree is **disposable** when it has no
change, untracked file or operation under way, no process has its working directory, an open file
or an argument inside it, and it holds no ignored files except caches (`__pycache__`,
`.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.hypothesis`, `.DS_Store`, `*.pyc`). Any other
ignored file, such as results, receipts, logs or captures under a build folder, may be evidence,
and keeps it.

**No worktree is removed**, disposable or not: `git worktree remove` deletes ignored files without
warning. Remove one through the project's documented disposal path. A locked worktree is never
listed: the queue worker's checkout, a retired one `qk disk` lists, and qk land's own scratch
worktrees, which `reclaim` handles as the next section says.

```sh
python3 scripts/qk queue reclaim --dry-run
python3 scripts/qk queue reclaim --json
```

`--dry-run` deletes nothing and says what it would. `--json` adds `branches` (as `branchCleanup`
above) and `worktrees` (`path`, `branch`, `head`, `entry`, `disposable`, `why`, and the first few
`evidence` paths with `evidenceCount`) to the scratch-worktree result. Kept branches and worktrees
are a report, not a failure, and do not change the exit status.

### Scratch worktrees an interrupted landing left

`qk land` checks a new entry, and replays a branch, in a locked scratch worktree of the repository
in a temporary folder, removed when it ends. A process killed in between leaves it, locked, and Git's
own pruning never takes a locked worktree. So each scratch worktree has a record, written before it
is added, under the queue's folder in the Git directory: the repository, the path, this Mac, the PID
and start of the process that made it, and what it was for. Its lock's reason names the record.

Each worker removes what it can when it starts, and any session may run:

```sh
python3 scripts/qk queue reclaim [--dry-run] [--json]
```

It removes a scratch worktree only when its record names this repository and this Mac, its maker
has ended (its PID is gone, or runs a process started at another time), the registration's lock
names the record, no process uses it, it holds no change, untracked or ignored file and no
operation under way, and each commit of its HEAD that no ref holds has an equal patch on
origin/main. It removes it without `--force`, and its folder only when nothing else is in it, since
a preflight lane's log there is evidence. A registration whose folder has gone is forgotten alone,
never by a `git worktree prune` of every registration. Anything else is kept with why. It exits 0
when nothing is left but worktrees whose makers still run, and 1 otherwise.

**Older scratch worktrees** were locked with `qk land's trial integration of a new entry, removed
when it ends` (or `eng land's`) and no record. Nothing proves who made them, so `reclaim` lists them
as `legacy` and removes none. The owner resolves each by hand, by its path, never by unlocking or
pruning every worktree:

1. `lsof +D PATH` and `ps -Aww -o pid,args | grep PATH` find no process using it.
2. `git -C PATH status --porcelain --ignored` prints nothing.
3. `git -C REPOSITORY cherry origin/main HEAD_SHA` prints no `+` line, for the HEAD that
   `git worktree list` shows.
4. `git -C REPOSITORY worktree unlock PATH` and `git -C REPOSITORY worktree remove PATH`, without
   `--force`, then remove its empty temporary folder.

**The old store's app source.** Before Queuekeeper had its own names, `qk app` kept its source
worktree under `~/Library/Application Support/vocaro-engineering/apps/NAME/source`. A Mac moved
since lists that registration, locked, with its folder gone. From the project, `qk app status` names
it, and `qk app retire-old-source [--dry-run]` forgets it once the store's own `source` worktree
replaces it, registered and locked, and its HEAD is on origin/main. It touches nothing else of the
old store, and exits 1 saying why when it keeps it.

### Registered Mac builds

Xcode registers a Mac app when it builds it, and macOS when it launches it, and neither is undone.
Stale registrations make the system run an old build's plugins, document types or URL schemes.

So when `qk run` or `qk admit` ends, however it ends, Queuekeeper unregisters each Mac app
(`lsregister -u`) and app extension (`pluginkit -r`) under the derived data its xcodebuilds named,
read from their `-derivedDataPath` or `-xctestrun` while the run goes. A lane needs no option, but:

- **Give each lane its own `-derivedDataPath`.** An xcodebuild without one is not covered (the run
  says so), and a folder another live run uses is left until it is free.
- **Nothing else is touched**: nothing outside the folder or through a link, no iOS app, and never
  a folder that cannot be derived data, such as the home folder, a checkout or Xcode's shared
  default.
- **A killed run** leaves a journal that the next run on that Mac sweeps.

The run prints one line on stderr saying what it unregistered and what it left. A problem here never
changes the run's result.

## Further reading

- [Related tools](related_tools.md) compares Queuekeeper with other merge queues.
- The plugins: [Apple](../plugins/apple/src/queuekeeper_apple/README.md),
  [Android](../plugins/android/src/queuekeeper_android/README.md),
  [Mobile](../plugins/mobile/src/queuekeeper_mobile/README.md),
  [Unity](../plugins/unity/src/queuekeeper_unity/README.md) and
  [Mac desktop](../plugins/mac-desktop/src/queuekeeper_mac_desktop/README.md).
- The [developer guide](developer_guide.md) for how each part works inside.
