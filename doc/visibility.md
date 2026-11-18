# Visibility

How the queue shows its owner what it is doing, beyond the command line and log files.

Queuekeeper's users are mostly agents, and they read exit codes and status lines. The owner needs
something else: to see at a glance whether the queue is moving, and to be told when it needs them.
Three features do that. Each is off until you turn it on, and each can be turned off, changed or
removed without touching the queue or the others.

| Feature | What it gives you | Command |
|---|---|---|
| `watch` | A live view of the queue in the terminal | `qk watch` |
| `notify` | A banner, a command or a webhook when the queue needs you | `qk notify` |
| `dashboard` | A read-only web page of the queue | `qk serve` |

All three read the same two things: the queue's [event journal](#the-event-journal), which is
always written, and its status.

## Turning features on and off

Every feature is off until you turn it on.

```sh
qk visibility status            # each feature, and whether it is on
qk visibility enable notify
qk visibility disable notify
```

What is on belongs to the owner and the Mac, not to a project, so it is kept in
`~/Library/Application Support/queuekeeper/visibility.json` and not in `queuekeeper.json`.

`QUEUEKEEPER_VISIBILITY=off` in the environment turns every feature off for the commands that see
it, whatever the file says.

## The watch feature

`qk watch` shows the queue in the terminal and draws it again every two seconds until
interrupted: what the queue is doing in one line, each running gate, the entries waiting, the last
few settled and the journal's last events.

```sh
qk visibility enable watch
qk watch                  # live, until Ctrl-C
qk watch --once           # print the view once
qk watch --interval 5 --events 20
```

Where its output is not a terminal, it prints the view once and exits, as with `--once`.

## The notify feature

Notify tells you when the queue needs you. Each time the queue journals an event that a rule is on
for, a separate process reads the journal from where the last one stopped and sends a notice
through every channel that is on. The queue does not wait for it.

```sh
qk visibility enable notify
qk notify status          # each rule and channel, and whether it is on
qk notify test            # send a test notice through each channel that is on
qk notify check           # look now for a queue that is stuck
```

**Rules** decide which events you are told of:

| Rule | Tells you when | Default |
|---|---|---|
| `held-owner` | The queue stopped for a cause only you can fix | On |
| `held-session` | The queue stopped for a cause an agent fixes | Off |
| `came-back` | An entry failed its gate, or conflicts with `main` | On |
| `stuck` | A gate is held up by dead admission entries, or the queue's doctor finds the queue stuck | On |
| `flaky` | A failed test passed alone | On |
| `ungated` | An entry landed without the gate | On |
| `landed` | An entry landed | Off |
| `resumed` | A hold was cleared | Off |

**Channels** are the ways of telling you:

| Channel | What it does | On when |
|---|---|---|
| `banner` | Posts a macOS notification | On a Mac, unless turned off |
| `command` | Runs your command with the notice as JSON on its standard input | Its `argv` is set |
| `webhook` | Posts the notice as JSON to your URL | Its `url` is set |

```sh
qk notify enable landed                     # a rule
qk notify disable banner                    # a channel
qk notify set webhook url https://example.com/hook
qk notify set command argv '["/usr/local/bin/tell-me", "--queue"]'
qk notify unset webhook url
```

The settings are the `notify` object of `visibility.json`. The `command` channel's `argv` is an
argument list and never shell source; the notice's title and body follow it as two more arguments.
The `webhook` channel's object has a `text` key, the notice in one line, which Slack-compatible
webhooks post as it is, and takes `headers` for a token. The `banner` channel takes a `sound`, the
name of a system sound.

What to expect:

- **Each event is told at most once.** Where a delivery stopped is kept beside the journal, and
  moves on before anything is sent, so a channel that fails or hangs is not tried again for the
  same event.
- **A channel that refuses a notice does not hold up the others.** The refusal is written to
  `notify.log`, beside `visibility.json`.
- **A burst is gathered.** More than five notices at once go as four and one that counts the rest.
- **History is not news.** The first delivery tells of nothing older than two minutes.
- **Banners come from Script Editor.** macOS attributes a banner posted with `osascript` to it:
  the banner carries its icon, its notifications must be allowed in System Settings, and clicking
  the banner opens Script Editor. A banner shows for a few seconds and then stays in Notification
  Center.
- **A queue that stalls is found by looking.** Entries that wait with no worker, or gates in a row
  with no verdict, write no event. So each command that reaches the Macs, such as `land`, `run`
  and `queue`, starts a look at what `qk queue doctor` finds, at most once every two
  minutes for a repository. A finding is told once it has lasted five minutes, and again only
  after it has gone and come back. `qk notify check` looks now.
- **Looking needs a command to run.** With the signed launcher set up, one runs every two minutes.
  Without it, a queue nobody touches is not looked at until the next command.

A rule is an entry in `rules.py`; a channel is a module of its own in `notify/channels/`, with a
`ready(config)` and a `send(notice, config)`. Adding either changes no other file.

## The dashboard feature

`qk serve` serves a read-only web page of the queue at `http://localhost:8750/`. It opens on
every repository at once, and the picker in its header narrows any tab to one repository.

```sh
qk visibility enable dashboard
qk serve --open           # serve, and open the page in the browser
qk serve --port 9000
qk serve ../other-project # show another repository too
```

A dashboard already serving the port is replaced: `qk serve` stops it and serves in its place,
so serving again after a change, or from another checkout, needs no hunt for the old one.
`--no-replace` refuses instead. A port some other program holds is refused, and `--port 0`
serves on any free one.

| Tab | What it shows |
|---|---|
| Now (home) | Every queue's health in one verdict, naming each repository that needs a look, with the day's numbers and the last 24 hours behind each; Needs attention, what is open and needs someone (a hold, a gate that looks stuck, waiting with no worker, an entry that came back and has not been queued again, a test found flaky, another tab's warning), each linked to what acts on it; the flow from agents through each repository's queue and gates to the Macs and main, each entry counted for the coding agent whose session queued it (Claude or Codex), else for the agent whose worktree it came from, else for its branch's prefix, else for a Person; and the Queue: every round being tested, each gate with a ring of how far it is into its usual time, then what waits, numbered in the order it is taken. Rows link to entry and gate details |
| Farm | Each Mac's admission slots, what holds them and what waits, for every project, with its CPU's busy share against its limit, its load, its memory pressure and whether its screen is locked; recorded physical iOS reservations, owners and expected release, with connection status and transport from Xcode. Running and waiting requests link to admission details; persistent human-friendly IDs appear below their labels |
| Trends | The last 7 or 30 days against the ones before: entries landed per day, the share of what settled that came back, gate time as a share of each repository's usual, and flaky tests found; a row for each repository with its day against yesterday and its passing gates' times on one scale, today's lane runs on each Mac, and landings by day and hour. A gate's time is its repository's own, so across repositories each run is measured against its own repository's usual |
| Activity | What the queues did, newest first: a row for each round with how each of its entries ended (landed, came back and why, tested again), a chip for each lane it ran that opens its log, and the tests found flaky at its commit, and a line for each hold and resume between them; filtered to what came back or landed, or searched by subject, branch, commit, lane or test |
| Logs (a page) | Reached from a lane's chip, a failure or an entry rather than a tab: the log's round's other logs beside it, and the log read with its failing tests and their reruns, a bar of the whole log that marks failures and goes to them, passing suites folded, and a search of the whole file; it follows new lines while you are at the bottom. Every log is one link away |

The page asks for its tab's data every two seconds, so it follows the queue without being
reloaded.

**Repositories.** The page shows the repository `qk serve` was started in, those given on
its command line, and every repository whose queue has journaled an event while the feature was
on. It opens on all of them at once, at `/` and `/all/TAB`. The picker in the header narrows the
tab shown to one repository, at `/r/NAME/TAB`, and its first entry, All repositories, widens it
again; either way the tab is kept. The Farm tab is the same either way, since admission is a
Mac's and not a project's. A repository that cannot be read is said so beside the others.

**What it cannot do.** The server reads and never writes: it answers no request but GET, and the
page has no button that changes the queue. Removing an entry or clearing a hold stays on the
command line.

**Who can read it.** The server binds `127.0.0.1`, so only this Mac reaches it, and it answers
only a request that asks for this Mac by name, so a page on another site cannot read the queue
through your browser. `--bind ADDRESS` serves another address, and warns that every machine able
to reach it can then read the queue and its logs: there is no sign-in.

**Panels.** Each tab is a panel: `panels/NAME.py`, with a `TITLE`, an `ORDER`, a
`data(repository, query)` and an `everywhere(repositories, query)`, and `panels/NAME.js`, which
draws what either returns. `everywhere` answers for every repository at once; Queue and History
build theirs by combining each repository's own `data`, so the two views cannot drift apart. A
panel without `everywhere` has no tab where every repository is shown. A `summary` function gives
the panel a line on Home. A panel is found by its name, so adding one changes no
other file, and one that fails shows its fault in its own tab while the others go on.

**Limits.**

- The Farm tab reads each Mac about every ten seconds, since each question to another Mac is a
  connection over SSH.
- A server started where macOS denies local-network access, such as from some background
  launchers, shows another Mac as unreachable. One started from a terminal reads it.
- The page polls. A change shows within two seconds, not at once.

## The event journal

The queue appends one JSON object a line to `events.jsonl`, beside its other records in the
repository's Git directory, each time its state changes. The queue writes the journal and reads
nothing back. A journal that cannot be written is skipped and never stops a round.

```sh
qk queue events                       # everything, oldest first
qk queue events --follow              # then each new event as it happens
qk queue events --since 2026-11-12    # from a date or time
qk queue events --kind entry.         # every kind that begins with entry.
qk queue events --json                # the journal's own lines
```

Every event has these keys, and its kind's own beside them:

| Key | Meaning |
|---|---|
| `v` | The schema's version, 1 |
| `id` | Unique in the journal |
| `time` | Seconds since the epoch |
| `kind` | What happened |

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

A key with nothing to say is left out. The schema only grows: a later version adds kinds and keys
and never renames or removes one, so a reader ignores what it does not know.

The journal starts a new file at 4 MB and keeps the one before it, as `events.1.jsonl`.

The queue's state at one moment, as opposed to its changes, is `qk queue status --json`.
In Python that is `merge_queue.snapshot(root)`.

## How features stay separate

The rules that keep one feature's faults and changes to itself:

- **A feature is a module or package of its own** in `queuekeeper.visibility`, found by its name.
  Adding one changes no other file, and deleting its folder removes it.
- **A feature reads only the journal and the snapshot.** A feature that wants to act on an event
  registers a hook that the journal calls after writing it. The hook must return at once, and
  what it raises is dropped, so a feature that is slow or broken cannot slow or stop a round.
- **A feature imports the core and never another feature**, and the core imports none of them. A
  test fails a change that breaks this.
- **A feature that is off is never imported**, and adds no command.
- **A feature that fails to load is reported and left out.** The command that was asked for still
  runs.

## Writing a feature

A feature is a module `queuekeeper/visibility/NAME.py`, or a package `queuekeeper/visibility/NAME/`,
with a docstring whose first line says what it does and a `register()` function. `register()` runs
only while the feature is on, and adds the feature's commands as an extension does. To read the
queue, a feature calls `events.read`, `events.follow` and `merge_queue.snapshot`. To act on an event
as it is written, it adds a function to `extensions.AFTER_EVENT`.

The three features are the examples: `watch.py` is one file, `notify/` adds rules and channels,
and `dashboard/` adds panels.

See also [The merge queue](merge_queue.md) and [The agent interface](agent_interface.md).

Admission names in the dashboard use the same `qk7318-description` format as queue entries, with
branch and explicit issue information when available. The dashboard keeps their names in
`dashboard-admission-ids.json` beside its repository registry. Internal admission lock tokens stay
unchanged, and existing links using those tokens still work.
