<p align="center">
  <img src="assets/banner.svg" alt="Queuekeeper" width="100%">
</p>

Queuekeeper is a merge queue and build farm for one developer working with AI coding agents. It
runs on that developer's own Macs instead of in the cloud. Agents hand it commits; it batches them,
tests each batch once on whichever Mac has room, and pushes to `main` only what passed.

```sh
queuekeeper land --wait     # queue HEAD and follow it: 0 landed, 1 came back, 75 held or still waiting
queuekeeper queue status    # what is waiting, what is testing, what came back and why
```

It has two parts:

- **The core** is the queue, a lane runner, admission control that keeps concurrent agents from
  overloading a Mac, and placement of work across several Macs over SSH.
- **Plugins** add knowledge of a platform's tools. The Apple plugin, the only one so far, manages
  simulator pools, physical-device reservations, Xcode test runs and the dialogs and permissions
  that stop an unattended Mac from finishing a test run.

## Why another merge queue?

Several good merge queues exist, and for most teams one of them is the right choice. Queuekeeper
was built for a case they fit poorly:

- **One developer, many agents.** A single developer directing AI agents can produce dozens or
  hundreds of commits a day. The author's six repositories each landed 8 to 14 times a day over two
  weeks, and in the busiest, 82% of landings were followed by another within 45 minutes. A gate
  that restarts whenever `main` moves never finishes at that rate, so the changes need a queue. They
  do not need pull requests or reviews, which are built for a team.
- **Builds that need local tools.** Cloud queues and build farms suit web services and large teams.
  They suit mobile apps, games and other front-end work less well, because those need Xcode, Unity or
  Unreal, simulators and real devices, which are difficult or expensive to run in the cloud. A
  GitHub-hosted macOS runner costs $0.062 a minute, so a 30-minute gate costs $1.86, and a hosted
  runner has no screen to unlock and no iPhone plugged into it.
- **Platform knowledge.** Running iOS tests unattended takes more than calling `xcodebuild`. Agents
  must not share a simulator, a device must be used by one run at a time, a crash dialog must not
  cover the window under test, and a locked screen must not be mistaken for a test failure. That
  knowledge belongs in the tool, which is what the plugins are for.
- **Hardware you already own.** The build farm is the Mac on your desk plus any other Mac you can
  reach over SSH. Nothing has to be rented. A rented Mac with SSH also works as an overflow host.

## How it compares

Read from each tool's own documentation and pricing pages in November 2026; follow the links to
check for changes. "Not documented" means the documentation does not mention it.

| | Queuekeeper | [GitHub merge queue][gh] | [Mergify][mergify] | [GitLab merge trains][gitlab] | [Trunk][trunk] | [Aviator][aviator] | [Graphite][graphite] |
|---|---|---|---|---|---|---|---|
| **Runs where** | Your Macs | GitHub | SaaS; self-hosted on Enterprise | GitLab.com or self-managed | SaaS | SaaS; self-hosted on Enterprise | SaaS |
| **Code host** | Any Git remote | GitHub, organization repositories | GitHub | GitLab | GitHub; GitLab.com in beta | GitHub | GitHub, organization repositories |
| **What you queue** | Commits and branches | Pull requests | Pull requests | Merge requests | Pull requests | Pull requests | Pull requests |
| **Code review, approvals** | None | Yes | Yes | Yes | Yes | Yes | Yes, with stacked pull requests |
| **Who runs the tests** | Queuekeeper, on your Macs | Your CI | Your CI | GitLab CI | Your CI | Your CI | Your CI |
| **Your own Mac as the builder** | Built in | Through a self-hosted Actions runner | Through your CI's self-hosted runner | Through a GitLab runner, documented for iOS | Through your CI's self-hosted runner | Through your CI's self-hosted runner | Through your CI's self-hosted runner |
| **Batching** | Yes: everything waiting is one batch | Merge groups; each build still runs | Yes | No | Yes | Yes | Private beta |
| **Speculative parallel checks** | One gate stacked on the running one | Yes, up to 100 | Yes, up to 128 | Yes, 20 by default | Yes | Yes | Yes |
| **Finding the bad change in a batch** | Bisects, both halves at once; skips the bisect when the failure points at one entry | Removes the failing pull request and rebuilds | Bisects | Removes the failing merge request and restarts | Bisects | Bisects | Isolates or bisects |
| **Flaky tests** | Reruns the failed tests alone; lands a proven flake and records it (Python test lanes only) | Not documented | Later batches can vouch for earlier ones; quarantine in a separate product | Not documented | Optimistic merging; retries failures that match flaky patterns | Classifies a failure and reruns the job once (beta) | Not documented |
| **Priorities** | Jump to the front | Jump to the front | Priority rules | Not documented | Levels 0 to 255 | Yes | Fast-track |
| **Splitting by affected paths** | Documentation only | No | Scopes, with Bazel and Nx | No | Parallel queues, with Bazel and Nx | Affected targets, with Bazel and Nx | Not documented |
| **Pause** | Holds itself with a reason; no manual pause | Not documented | Pause and scheduled freeze | Not documented | Pause, drain, resume | Pause per repository or branch | Pause |
| **Simulators, devices, Xcode** | Apple plugin | Not documented | Not documented | Runner setup guide only | Not in the queue | Not documented | Not documented |
| **Admission by machine load** | Yes | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI |
| **Interface** | CLI with exit codes and JSON | Web, `gh`, API | Web, CLI, API, agent skills | Web, API | Web, CLI, API | Web, API | Web, CLI |
| **Price** | Free, Apache 2.0 | Included; private repositories need Enterprise Cloud | Free up to 5 users; $21 a seat a month | Premium, $29 a user a month | Free up to 5 committers | From $20 a developer a month | From $40 a user a month |
| **Used by** | Its author | GitHub and many large projects | Many teams | Many teams | Many teams | Many teams | Many teams |

[gh]: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
[mergify]: https://docs.mergify.com/merge-queue/
[gitlab]: https://docs.gitlab.com/ci/pipelines/merge_trains/
[trunk]: https://docs.trunk.io/merge-queue
[aviator]: https://docs.aviator.co/mergequeue
[graphite]: https://graphite.com/docs/graphite-merge-queue

Also worth knowing: [Zuul](https://zuul-ci.org/) gates changes across many repositories at very
large scale and is heavy for one person to run. [bors-ng](https://github.com/bors-ng/bors-ng), the
original batching queue, was archived in 2024. [Kodiak](https://github.com/chdsbd/kodiak) and
[Bulldozer](https://github.com/palantir/bulldozer) merge pull requests automatically but test
nothing themselves.

### Other local queues for agents

Queuekeeper is not the only local merge queue written for AI agents. These queue commits instead of
pull requests and run a check command on your machine:
[merge-queue](https://github.com/chlwys/merge-queue),
[mergetrain](https://github.com/Drezzler/mergetrain-pr),
[claude-code-merge-queue](https://github.com/funador/claude-code-merge-queue),
[jjq](https://pauladamsmith.com/blog/2026/02/introducing-jjq-a-local-merge-queue-for-jj.html) for
Jujutsu, [SelfCI](https://docs.rs/crate/selfci/latest/source/README.md) and
[sluice](https://github.com/agentplot/sluice) (not evaluated here). They are much smaller and
simpler to set up, and several offer an MCP server, which Queuekeeper does not. As documented, each
runs on one machine, and none describes bisection, flaky-test handling, admission by load, or
anything about simulators or devices. If one machine and one check command are enough, start with
one of them.

### Where the others are better

- **Teams.** Reviews, approvals, branch protection, single sign-on, audit trails and dashboards.
  Queuekeeper has none of these and assumes everyone who can queue a commit is trusted.
- **Throughput.** The hosted queues test many speculative batches at once across as many CI
  machines as you pay for. Queuekeeper runs one gate, one gate stacked on it, and the two halves of
  a bisect, on a handful of Macs.
- **Large monorepos.** Mergify, Trunk and Aviator split a queue by affected targets and integrate
  with Bazel and Nx. Queuekeeper only separates documentation from code.
- **Clean, elastic builders.** A hosted runner starts fresh every time and never depends on a desk
  Mac being awake, logged in and unlocked.
- **Flaky-test history.** Trunk, Mergify and Aviator track flaky tests across runs and can
  quarantine them. Queuekeeper proves a flake by rerunning it, for one kind of lane.
- **Maturity.** They have many users, support and a web interface. Queuekeeper has one user and a
  command line.

### You may not need a new queue

A hosted queue can already drive your own Mac. Mergify's and Trunk's free tiers cover a solo
developer's private repositories, and both work with a GitHub Actions self-hosted runner on a Mac.
GitHub's own queue cannot do this for a personal account: it needs an organization-owned repository,
and Enterprise Cloud when that repository is private. GitLab documents Mac runners for iOS work, with
merge trains on its Premium tier.

That setup still goes through pull requests and a CI service, and the queue knows nothing about the
Mac it runs on. The gaps Queuekeeper fills are admission that counts everything the agents run and
not only gates, placement across Macs by measured load, a screen-lock check, and the Apple plugin's
simulator pool and device reservations.

## Getting started

Requirements:

- **macOS.** Queuekeeper reads macOS's load, memory-pressure and screen-lock state and has not been
  ported to Linux or Windows.
- **Python 3.14 or newer**, with no other Python packages.
- **Git**, and a repository whose changes land directly on `main` of a remote named `origin`.
- **Optional: more Macs**, each reachable by SSH key, with Python 3.14 or newer.
- **For the Apple plugin:** full Xcode, the same build on every Mac. Developed against Xcode 27.2.

Installation, a first `queuekeeper.json` and adding a second Mac are covered in
[Getting started](docs/getting-started.md).

## The core

### Landing a commit

An agent commits its work on any branch and runs `queuekeeper land`. That queues the commits the
branch has and `origin/main` lacks, held by a Git ref of their own, so the agent's session can end
and its branch can move or be deleted. One worker per repository does the rest.

Each round, the worker:

1. fetches `origin/main` and takes everything waiting, oldest first, as one batch;
2. cherry-picks the batch onto `origin/main` in a checkout of its own;
3. runs the project's gate lanes once for the whole batch, on whichever Mac has room;
4. pushes exactly the commits it tested, each with its own message and author.

Queue state is kept in the repository's Git directory, so every worktree sees the same queue. A
worker that dies loses nothing: whatever it was testing was never pushed, and the next worker tests
it again. A batch that passed is recorded before its push, so an outage at the remote does not
discard the result.

### Checked before it is queued

`queuekeeper land` refuses an entry in seconds, with nothing queued, when a round would only send it
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

### When a batch fails

The worker tries the cheap explanations before the expensive one:

1. **Flake.** It reruns each failed test alone, up to three times. If every rerun passes, and no
   commit in the batch changes the test's module or a file that module imports, the batch lands.
   The flake is recorded in the queue's status, in a `Flaky-test:` trailer on the landed commit, and
   as a comment or new issue on the tracker.
2. **One obvious culprit.** If the failure is real and every failed test points at the same entry,
   by a path its output names or a file it imports, that entry is sent back and the rest gate again
   at once.
3. **Bisect.** Otherwise the batch is halved until one entry fails alone. Both halves gate at the
   same time, on different Macs, and every entry that passes along the way lands.

Steps 1 and 2 read test names from the output of `queuekeeper unittest`, the bundled Python test
runner. A failure in any other kind of lane goes straight to the bisect.

### While a gate runs

- **Stacked gates.** Entries that arrive during a gate are tested on top of the batch's tip, on
  another Mac. If the batch lands, the stacked gate becomes the next round's gate and is already
  under way. If it does not, the stacked gate is thrown away. Only one runs at a time.
- **Documentation does not wait for code.** An entry that changes only Markdown documents lands
  beside a running code gate, after the documentation lanes pass. If `main` moves only by documents
  while a code batch gates, the batch's result carries over without a second gate.
- **Removing an entry** stops its gate within seconds and frees the Mac.

### No verdict is not a failure

A lane ends as passed, failed or unmeasured. A timeout, a lost connection, a Mac too busy to admit
the lane, a misconfigured host and a screen that locked during a UI test are all unmeasured: they
say nothing about the commits, so nobody's entry is sent back. The worker waits for the Mac to
settle (CPU busy share under 75%, normal memory pressure) and runs the lane again.

After three runs in a row with no verdict, the queue holds with every entry still waiting. A hold
states its reason and who can clear it: an agent for most causes, the owner only for causes an agent
cannot fix, such as a refused SSH key. Whoever fixes the cause runs `queuekeeper queue resume`.

A remote that cannot be reached is retried with backoff for about an hour before the queue holds.
During a GitHub incident, the worker reads GitHub's status page and keeps trying while Git
operations are degraded.

### Emergencies

- `queuekeeper land --front` tests an entry next, ahead of everything waiting.
- `queuekeeper land --no-gate --why "..."` lands with only the lint lane and the pre-push hook. The
  reason is required and is recorded in a `Landed-without-gate:` trailer. Status lists the commit as
  ungated until a later gate covers it.

### Lanes

A lane is a named command in `queuekeeper.json`, written as an argument list, with a timeout and a
statement of what it needs from the machine:

```json
{
  "lanes": {
    "unit":    {"command": ["queuekeeper", "unittest", "-s", "tests"], "timeout": 1800},
    "ui":      {"command": ["scripts/ui-tests.sh"], "timeout": 3600, "unlockedScreen": true},
    "measure": {"command": ["scripts/benchmarks.sh"], "resource": "exclusive"},
    "gate":    {"parallel": ["unit", "ui"], "budgetMinutes": 20}
  },
  "queue": {"lanes": ["gate"]}
}
```

`queuekeeper run LANE` runs one, for an agent as for the queue. Each run leaves a log and a receipt
that records the commit, the command, the exit status, the outcome, the Mac it ran on and how long
it waited. Results of whole-lane runs at a clean commit are kept by commit, so any worktree can ask
whether that commit already passed a lane.

A lane can require a minimum number of executed tests, so a run that silently selects nothing fails.
Test counts are read from unittest, pytest, XCTest and Swift Testing output. A lane group runs its
members side by side under one admission. `budgetMinutes` warns when the gate's median time drifts
past the target, and names the slowest parts.

### Admission

Ten agents that each start a build at the same moment make every build slow and every timing test
meaningless. Every lane, and any command run through `queuekeeper admit -- COMMAND`, therefore asks
the Mac for a slot first, in one first-come, first-served queue for the whole machine.

- Three slots are always available. Further slots, up to half the cores, open one at a time while
  the CPU busy share is under 75% and memory pressure is normal.
- A lane can ask for the whole host, for work that starts its own parallel jobs, or for exclusive
  use, for timing measurements. One lane at a time may use an on-device model or the GPU.
- A waiting lane says what it is waiting for and behind whom, and repeats it every five minutes.
- `queuekeeper admissions now` shows who holds slots and who is waiting on every Mac. An admission
  log records every wait and hold.

Admission cannot preempt running work, and it cannot see work that never asks, such as an editor's
background indexing.

### Several Macs

A lane is offered first to another Mac and runs on the starting Mac when the other has no room. When
neither has room, it runs on whichever admits it first. The tool decides, by reading each Mac's free
slots, CPU busy share and memory pressure. The agent does not.

- **Shipping the code.** The exact commit, or a snapshot commit of the working tree, is pushed over
  SSH straight to the other Mac. Nothing goes through the Git host. The other Mac keeps four
  checkouts per project and reuses the one closest to the new commit, so builds stay warm. A
  one-line change sent 282 bytes in one measurement.
- **Surviving a dropped connection.** A run is journaled on the Mac that executes it. If the
  starting Mac sleeps or the connection drops, the run continues and the starting Mac reattaches and
  collects the result.
- **Unreachable hosts.** A Mac that cannot be reached is skipped and the lane runs locally. A Mac
  that is reachable but set up wrongly, with a different Xcode build for example, runs nothing and
  says so, instead of producing results that cannot be compared.
- **Gate hosts.** The queue's gates can use a list of Macs, tried in order. A rented Mac with SSH
  can be one of them.
- **macOS Local Network privacy.** macOS can silently deny local-network access to a background
  process whose parent has exited. Queuekeeper detects this instead of reporting the other Mac as
  down, and includes a small signed launcher that avoids it.

Environment variables do not travel to the other Mac, so lane settings must be arguments.

### Screen lock

Tests that drive a user interface fail in confusing ways behind a locked screen. A lane marked
`"unlockedScreen": true` runs only on a Mac whose screen reads unlocked and waits while none does. A
run that ends unmeasured with the screen locked is not counted against the commits. Queuekeeper
cannot unlock a screen.

### Process supervision

Every lane runs in its own session and process group with a time limit. When a lane ends, times out
or is stopped, Queuekeeper takes a census of what it started and stops what is left. If something
survives, the run is marked uncertain and its slot stays held until that is settled, so the leftover
load is never hidden. An entry whose owner has provably finished is recovered automatically.

### Built for agents

- **Exit codes** separate the cases an agent must treat differently:

  | Code | Meaning |
  |---|---|
  | 0 | Passed, or landed |
  | 1 | Failed, or came back |
  | 2 | Usage or configuration error |
  | 3 | Unmeasured |
  | 4 | Failed before any test started |
  | 75 | Busy or held; nothing ran |
  | 78 | A host is misconfigured |

- **Fixed status lines** end every run with the verdict, the test counts, up to twenty failing
  tests with file and line, and the path of the full log.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `queue status`, `queue doctor`, `queue placement`, `admissions now` and
  `doctor`.
- **Nothing prompts.** Commands are meant to run in the background and be followed to their result.

`queuekeeper queue doctor` reads the queue's own records and says whether it is testing or stuck,
and what would unstick it. `queuekeeper doctor` checks a project's lanes and every Mac's setup.

## The Apple plugin

The Apple plugin is optional, and the core runs without it. It was developed against Xcode 27.2,
iOS 27 simulators and macOS 27, and parts of it depend on behavior Apple does not document, which a
new release can break.

### Simulator pool

Scripts that pick a simulator by name, or use `booted`, end up on Xcode's default simulators or on
one another agent is using. The pool gives each run its own.

- **Taking a simulator.** `queuekeeper simulator-pool take --device iPhone-17-Pro --runtime iOS-27-0`
  reserves a free member of that kind, or creates one, and prints its identifier. Reservations go
  through one locked record per Mac, so two agents cannot take the same simulator. `check` refuses
  any simulator outside the pool.
- **Warmed templates.** A new simulator's first boot is slow, and its first query for system voices
  took 285 to 344 seconds in measurements, and far longer under load. The pool warms one template
  per kind and resets each member by copying the template's data with APFS clones. Copying a 2.2 GB
  template took 0.24 seconds, and clones answered the voice query in 11 to 13 seconds.
- **Cleaning up.** A simulator left booted with no live reservation is shut down after two hours.
  Members unused for an hour are deleted, except the first of each kind.

Each Mac has its own pool. A simulator's identifier means nothing on another Mac, so a command that
names one stays on the Mac it was started on.

### Simulator tests

`queuekeeper simulator-tests --scheme App --runtime iOS-27-0 --device iPhone-17-Pro --device iPad-Air-13-inch-M4`
builds once and tests on each device type at the same time, each on its own pool simulator, with its
own log and result bundle.

- `--shards N` spreads one device's tests over N simulators, packed by how long each class took in
  the last run. Xcode's own parallel workers do not spread Swift Testing suites this way.
- `--repeat N` builds once and runs N times on fresh simulators, to show that a fix for an
  intermittent failure holds.
- Each device's outcome is reported separately. The run fails if any device failed, and is
  unmeasured if any device was.

It covers iOS simulators only, one runtime per run.

### Swift package tests

`swift test --parallel` reports a skipped XCTest test as passed and prints no total.
`queuekeeper xctest` builds once, packs XCTest classes into shards by their last run times, runs the
shards side by side and prints one line with the counts of tests run and skipped. A shard that ran a
different number of tests than it was given counts as failed.

For lanes that call `xcodebuild` themselves, `queuekeeper results keep` reads a result bundle and
records each test's outcome and duration in the same form.

### Physical devices

An iPhone can run one test session at a time, and nothing in Xcode stops two agents from installing
to it at once.

- **Reservations.** Each Mac keeps a record of who holds each device and until when. Agents take a
  device in turn, first come, first served.
- **Bounded runs.** A command run under a reservation is stopped before the reservation ends, with
  time left to restore the device. A command that names a different device is refused.
- **Across Macs.** A device paired with two Macs is reserved in one record, and the reservation
  travels with a run sent to the other Mac.
- **Notifications.** A banner that arrives mid-run moves VoiceOver's focus and puts private text on
  screen. The plugin runs a Shortcut on the device to silence notifications, watches the device log
  for banners, and marks a run that one reached as unmeasured.

The record coordinates only work that goes through Queuekeeper. A person running `xcodebuild` by
hand bypasses it. It currently tracks one iPhone and one iPad.

### Unattended Macs

A UI test on a Mac nobody is watching fails for reasons unrelated to the code. The plugin handles
the ones met so far:

| Problem | What the plugin does |
|---|---|
| Two UI runs fight over one screen and keyboard | Reserves the Mac's foreground for one run at a time |
| A "quit unexpectedly" alert covers the window under test | Suppresses crash dialogs for the run, dismisses any that appear, restores the setting afterward |
| A notification banner covers the screen | Closes it. If it cannot, the run is unmeasured |
| The login keychain is locked in an SSH session, so code signing fails | Unlocks it for each run from a password the owner stored once on that Mac |
| macOS asks for screen-capture approval again every 30 days | Launches the test runner from one stable path so the approval persists, and reports when it will lapse |
| Every built copy of a Mac app stays registered, and the system launches an old one | Unregisters the copies a run built |
| VoiceOver is left on, or toggled late, after a run | Saves and restores its state |

When a guard cannot clear a problem, the run ends unmeasured instead of failed. The keychain guard
stores a login password on the build Mac, readable only by that account. Use it only on a Mac that
holds nothing sensitive.

### Keeping Macs alike

Test results from two Macs are comparable only if the tools match. `queuekeeper doctor` compares the
Xcode build and the installed simulator runtimes on every Mac. It evens out the runtimes by copying
a runtime image to the Mac that lacks it, or deleting one that nothing uses. `queuekeeper disk`
lists stale DerivedData folders, SwiftPM caches and unused runtimes, and deletes them on request.

## Limitations

- **One user so far.** Queuekeeper was extracted from its author's private tooling. Expect rough
  edges, and defaults that suit one person's setup.
- **macOS only**, for the queue and for every build host.
- **One plugin.** There is no Android, Unity or Unreal plugin. The core runs any command as a lane,
  so those builds can be queued, but nothing manages emulators, devices or editors for them.
- **No pull requests.** Queuekeeper lands directly on `main` and refuses a project set up for pull
  requests. There is no review step.
- **Flake detection covers Python test lanes only.** Failures in other lanes are bisected.
- **Two Macs outside the gate.** The queue's gates can use a list of Macs. A lane an agent runs
  directly is offered to one other Mac.
- **A build host can be asleep, locked or busy.** Queuekeeper reports these as no verdict and waits,
  which is correct but slower than a machine that is always ready.
- **No web interface**, dashboards or notifications. Everything is the command line and log files.

## License

[Apache License 2.0](LICENSE).
