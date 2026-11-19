# Queuekeeper developer guide

How Queuekeeper works inside, and how to change it. Read the [user guide](user_guide.md) first:
this guide assumes you know what a lane, a gate, admission and the farm are.

- [Working on Queuekeeper](#working-on-queuekeeper): the rules for changing this repository
- [Code map](#code-map): where each part lives
- [How it fits together](#how-it-fits-together): a landing end to end, admission and placement,
  plugins
- [Scheduling](#scheduling): the goals every scheduling rule serves
- [Visibility features](#visibility-features): the event journal, and writing a feature
- [Checks this repository runs](#checks-this-repository-runs): docs, citations, spelling,
  screenshots
- [Reference guides](#reference-guides): the detailed guides, and when to read each

## Working on Queuekeeper

[AGENTS.md](../AGENTS.md) has the rules every session follows. In short:

- **Edit only in a linked worktree.** `~/Development/queuekeeper` is the shared checkout every
  project's `qk` runs from, so it stays on `main`.
- **Every test and build goes through admission**, even a single test:

  ```sh
  python3 scripts/qk unittest -s tests integration.test_merge_queue.Landing
  python3 scripts/qk run gate
  python3 scripts/qk admit -- COMMAND
  ```

- **Land through the queue**, with `python3 scripts/qk land --wait --json` in the background.
  Never push to `main`.
- **Keep the parser and the schema in step.** A setting added to `config.py` goes into
  `schemas/queuekeeper.schema.json` too.

Changing Queuekeeper while other projects' work runs on it has its own rules:
[Changing Queuekeeper while work runs](guides/maintenance.md#changing-queuekeeper-while-work-runs).

## Code map

| Path | What it holds |
|---|---|
| `src/python/queuekeeper/cli.py` | Command dispatch and lane execution |
| `src/python/queuekeeper/merge_queue.py` | Submission, batching, workers and landing |
| `src/python/queuekeeper/agent_cli.py`, `agent_results.py`, `agent_logs.py`, `recovery.py` | Structured results, resumable log reads and recovery advice |
| `src/python/queuekeeper/resources.py` | Heavy-work admission on one Mac |
| `src/python/queuekeeper/remote.py`, `offload_host.py` | Placement and running work on another Mac |
| `src/python/queuekeeper/config.py`, `schemas/queuekeeper.schema.json` | Project settings |
| `src/python/queuekeeper/extensions.py` | Extension hooks. The core never imports a plugin directly |
| `src/python/queuekeeper/events.py` | The event journal |
| `src/python/queuekeeper/visibility/` | The visibility switch and features |
| `src/python/queuekeeper/docs.py`, `spelling.py` | The docs and spelling checks |
| `plugins/` | Separately packaged platform and visibility plugins |
| `queuekeeper-workspace.json` | Which plugins a checkout is composed of |
| `tests/` | Component suites, `integration/`, architecture checks and `test_support/` fixtures |
| `tools/` | Install script, screenshot script and other one-off tools |

Tests set up host isolation first (`import host_isolation`); follow the neighboring tests.

## How it fits together

### A landing, end to end

1. **`qk land`** finds the commits the branch has and `origin/main` lacks, replays them onto
   `origin/main` and every entry ahead in a scratch worktree, and runs the preflight lanes there.
   A conflict or finding refuses the entry with nothing queued.
2. **The entry is queued** under a Git ref of the queue's own, so the branch can move or vanish.
   Queue state is in the repository's Git directory, guarded by a lock, so every worktree shares
   it.
3. **A worker starts** if none runs: one per repository. Each round it fetches `origin/main`,
   takes every waiting entry as one batch, and cherry-picks it into a checkout of its own. It
   first follows Queuekeeper's own `origin/main`, so an old worker never misreads newer settings.
4. **The gate's lanes are placed and admitted** like any other lane (below). Entries arriving
   meanwhile are gated *stacked* on the batch's tip, on another Mac.
5. **On a pass**, the result is recorded, then the exact tested commits are pushed. A
   documentation-only batch skips the code gate and runs the docs lanes instead.
6. **On a failure**, the worker reruns failed tests alone, then looks for one culprit, then
   bisects, gating both halves at once on different Macs.
7. **On no verdict**, it waits for the Mac to settle and runs again; three in a row hold the queue.

Every change of state is appended to [the event journal](#the-event-journal). The full set of
rules, with the incidents behind them, is in [Maintenance](guides/merge-queue.md#the-merge-queue).

### Admission and placement

**Admission** is per Mac: one first-come, first-served queue that every lane, `qk admit` command
and gate passes through. Three slots are guaranteed; more open one at a time while the CPU is
under that Mac's `busyLimit`, memory pressure is normal and the Mac has settled since the last
admission. Whole-host and exclusive requests take every slot, and an older request holds later
ones back so nothing starves. Admission never preempts, and an owner that has died holds nothing.
Each Mac's numbers come from its own `admission-limits.json`; `qk admissions limits` prints
them.

**Placement** picks the Mac. Of the Macs with room, judged by each Mac's own admission rules,
the work goes to the one with the most headroom under its own CPU limit, with the farm's order
breaking ties. A waiting whole-host gate takes a place in the queue of every Mac that can run
it, runs on the first to admit it and gives up the others. What a Mac accepts (whole-host gates,
exclusive work, model runs, foreground work, labels) is in its own `mac-settings.json`, never in
a rule naming a Mac.

**Dispatch** pushes the exact commit over SSH into one of four warm checkouts per project on
the target Mac, runs the lane there under that Mac's admission, and journals the run on that Mac
so the sender can reattach after a dropped connection.

Detail: [Heavy-work admission](guides/admission.md#heavy-work-admission) and
[Which Mac runs a lane](guides/another-mac.md#which-mac-runs-a-lane).

### Plugins and extensions

A plugin teaches Queuekeeper a platform. A project lists its package in `extensions`, and the
plugin registers commands, lane behavior and checks through the hooks in `extensions.py`. The
core never imports a plugin, and platform behavior stays in its plugin. A plugin that fails to
load stops the run with exit 70 rather than running without it.

Each plugin has a README of its own: [Apple](../plugins/apple/src/queuekeeper_apple/README.md),
[Android](../plugins/android/src/queuekeeper_android/README.md),
[Mobile](../plugins/mobile/src/queuekeeper_mobile/README.md), which the Apple and Android plugins
share, [Unity](../plugins/unity/src/queuekeeper_unity/README.md),
[Mac desktop](../plugins/mac-desktop/src/queuekeeper_mac_desktop/README.md) and
[Mac hardware](../plugins/mac-hardware/src/queuekeeper_mac_hardware/README.md).

## Scheduling

Every scheduling rule serves these goals. Where two conflict, the earlier wins:

1. **A verdict is true.** A pass, failure or timing describes the commits, never what else the
   Mac was doing. Work that could not be measured runs again rather than passing or failing.
2. **Each Mac stays within its own limits.**
3. **Nothing waits without bound.** Requests are served in arrival order, and later or lighter
   work does not pass a request that has waited.
4. **Landings flow, and no Mac idles while work waits.** This is the objective the first three
   bound.
5. **A session's own run starts soon**, served by spare capacity, never by priority.

Two rules stand beside them. **The owner is not the scheduler**: Queuekeeper chooses the Mac and
the moment. **No Mac is special**: Macs differ only by their own settings.

[Scheduling](guides/scheduling.md) lists every rule in force with the decision behind it, the
decisions made against these goals, and how to weigh a proposed change. Read it before changing
admission, placement or the queue's order.

## Visibility features

The visibility features (watch, notify, the dashboard) are optional and isolated from the queue.
The [user guide](user_guide.md#watch-the-queue) covers using them.

### How features stay separate

- **A feature is a module or package of its own** in `queuekeeper.visibility`, found by name.
  Adding one changes no other file, and deleting its folder removes it.
- **A feature reads only the journal and the snapshot** (`merge_queue.snapshot(root)`, the same
  data as `qk queue status --json`). To act on an event as it is written, it adds a hook to
  `extensions.AFTER_EVENT`. The hook must return at once, and its exceptions are dropped, so a
  slow or broken feature cannot slow or stop a round.
- **A feature imports the core and never another feature**, and the core imports none of them.
  An architecture test enforces this.
- **A feature that is off is never imported** and adds no command. One that fails to load is
  reported and left out.

### Writing a feature

A feature is a module `queuekeeper/visibility/NAME.py` or a package `queuekeeper/visibility/NAME/`
with a docstring whose first line says what it does, and a `register()` function that adds its
commands as an extension does. It reads the queue through `events.read`, `events.follow` and
`merge_queue.snapshot`.

The three features are the examples:

- **watch** is one file.
- **notify** has rules and channels. A rule is an entry in `rules.py`; a channel is a module in
  `notify/channels/` with `ready(config)` and `send(notice, config)`.
- **dashboard** has panels. A panel is `panels/NAME.py`, with `TITLE`, `ORDER`,
  `data(repository, query)` and optionally `everywhere(repositories, query)` and `summary`, and
  `panels/NAME.js`, which draws what they return. A panel that fails shows its fault in its own
  tab while the others go on. The server answers only GET, binds `127.0.0.1`, and answers only a
  request that names this Mac, so another site cannot read the queue through a browser.

Admission names in the dashboard use the same `qk7318-description` format as queue entries, kept
in `dashboard-admission-ids.json` beside the dashboard's repository registry.

### The event journal

The queue appends one JSON object per line to `events.jsonl` in the repository's Git directory
each time its state changes. The queue only writes it, and a journal that can't be written never
stops a round. It rolls over at 4 MB, keeping the previous file as `events.1.jsonl`.

Every event has `v` (the schema version, 1), `id` (unique in the journal), `time` (seconds since
the epoch) and `kind`, plus its kind's own keys. A key with nothing to say is left out. The
schema only grows, so readers ignore what they do not know.

| Kind | When | Its own keys |
|---|---|---|
| `entry.queued` | A commit range joined the queue | `entry`, `subject`, `commits`, `branch`, `note`, `front`, `noGate`, `aheadWhy` |
| `entry.landed` | An entry is on `main` | `entry`, `subject`, `tip`, `landedAs`, `landedTip`, `landedCommits`, `docsOnly`, `ungated` |
| `entry.failed` | An entry came back from its gate | `entry`, `subject`, `tip`, `why`, `log` |
| `entry.conflict` | An entry came back with a conflict | `entry`, `subject`, `tip`, `why` |
| `entry.removed` | An entry was taken out | `entry`, `subject`, `tip` |
| `entries.testing` | Entries went under test | `entries` |
| `entries.requeued` | Entries went back to waiting | `entries` |
| `round.began` | The worker started a round | `round`, `head`, `base`, `entries`, `bisect`, `adopted` |
| `gate.began` | A lane of a gate started | `lane`, `round`, `role`, `head`, `entries` |
| `gate.ended` | A lane of a gate ended | `lane`, `round`, `role`, `status`, `log`, `mac`, `seconds` |
| `gate.stuck` | A gate's wait for admission is held up by dead entries | `lane`, `role`, `why`, `waitingSince` |
| `batch.passed` | A batch's lanes passed, before its push | `head`, `base`, `entries` |
| `batch.pushed` | A batch was pushed to `main` | `head`, `base`, `entries`, `ungated` |
| `test.flaky` | A failed test passed alone | `test`, `commit`, `issue`, `landed`, `entries` |
| `queue.held` | The queue stopped | `why`, `for` (`owner` or `session`) |
| `queue.resumed` | A hold was cleared | `was` |
| `queue.closed` | The queue was closed to new entries | `why`, `by` |
| `queue.reopened` | The queue takes new entries again | `why`, `by` (the closure that ended) |

## Checks this repository runs

- **Preflight: spelling.** `scripts/check_spelling.py` runs before every gate. It rejects British
  spellings and text that calls the command by its old package name before a subcommand. Fix a
  hit by spelling the word the American way or writing `qk`
  ([Spelling check](guides/maintenance.md#spelling-check)).
- **Docs check.** `python3 scripts/qk docs check` checks every Markdown link, anchor and
  `PATH:LINE` reference, and that each `qk WORD` a document tells a reader to type is a real
  subcommand ([Docs check](guides/maintenance.md#docs-check)).
- **Citations.** Queuekeeper turns on `"docs": {"citations": true}`, so the docs check also reads
  citations of a document by path and section title, such as
  `(doc/guides/admission.md, Heavy-work admission)`, in code, messages and the schema. Error
  messages cite the rule an agent broke, so it can read the reason. When you rename or move a
  section, the check names every citation to update.
- **The setup example.** `tests/core/test_setup_examples.py` parses each complete
  `queuekeeper.json` example in the [user guide](user_guide.md), since agents copy it. A
  documentation-only change to the user guide runs that test through the `setup-examples` lane
  (see `queue.unread` in [queuekeeper.json](../queuekeeper.json)).
- **The README's screenshots.** Retake them after a change to how the dashboard looks, by running
  `python3 tools/dashboard-screenshots.py` directly, not through admission, since it needs this
  Mac's Chrome ([The README's screenshots](guides/maintenance.md#the-readmes-screenshots)).

## Reference guides

These guides hold the full detail: every rule, the incident or measurement behind it, and the
messages it prints. Code and error messages cite them by section, so they are the source of
truth. Start with the user guide or this one, and come here for a specific mechanism.

| Guide | Read it when you are working on |
|---|---|
| [Scheduling](guides/scheduling.md) | Any change to admission, placement or the queue's order: goals, rules in force, decisions |
| [Merge queue](guides/merge-queue.md) | The merge queue's rules in full: batches, blame, pushes and hooks, docs-only batches, recovery, a project's first landing |
| [Maintenance](guides/maintenance.md) | Changing Queuekeeper while work runs, the signed launcher, the README's screenshots, the spelling and docs checks |
| [Heavy-work admission](guides/admission.md) | Slots, model admission, the admission log, who holds and who waits, trial runs |
| [Another Mac](guides/another-mac.md) | Which Mac runs a lane, what each Mac takes, how a run reaches another Mac, extension folders |
| [Screen state](guides/screen-state.md) | How a locked or asleep screen affects placement and the lanes that need it |
| [Setting up another Mac](guides/another-mac-setup.md) | `qk remote setup`, keychains on the right Mac, earlier configuration names |
| [Disk cleanup](guides/disk-cleanup.md) | `qk disk`, leftover processes, stale pool simulators, registered Mac builds |
| [Process supervision](guides/process-supervision.md) | Supervised processes, lock reclamation, stop signals, ending a coordinated command |
| [Unity editors](guides/unity-editors.md) | Running batches beside an open Unity editor |
| [Lanes](guides/lanes.md) | Receipts, test counts, compiling before tests, tests no lane runs, the gate's time budget |
| [Parallel tests](guides/parallel-tests.md) | Lane groups, `qk unittest`, `qk simulator-tests` and `qk xctest` |
| [Simulator pool](guides/simulator-pool.md) | The Apple plugin's reserved simulators and warmed templates |
| [Physical devices](guides/devices.md) | Device reservations, runs on a paired iPhone or iPad, the device pass, declared devices |
| [Kept app builds](guides/app-builds.md) | `qk app`, which keeps and runs the last good build of a project's app |
| [Foreground runs](guides/foreground.md) | Lanes that take a Mac's screen, attended sessions on this Mac |
| [Cloud gate runners](guides/cloud-gate-runners.md) | Renting a Mac as an overflow host |
