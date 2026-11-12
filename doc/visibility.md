# Visibility

How the queue shows its owner what it is doing, beyond the command line and log files.

Queuekeeper's users are mostly agents, and they read exit codes and status lines. The owner needs
something else: to see at a glance whether the queue is moving, and to be told when it needs them.
Visibility is built in layers so that each can be turned off, changed or removed without touching
the queue.

| Layer | What it is | State |
|---|---|---|
| Event journal | One line for each change of the queue's state | Built |
| `queuekeeper queue events` | The journal in the terminal | Built |
| `queuekeeper visibility` | The switch that turns features on and off | Built |
| `watch` feature | A live status view in the terminal | Built |
| `notify` feature | Banners, a command hook and webhooks | Planned |
| `dashboard` feature | A read-only web page on this Mac | Planned |

## The event journal

The queue appends one JSON object a line to `events.jsonl`, beside its other records in the
repository's Git directory, each time its state changes. The queue writes the journal and reads
nothing back. A journal that cannot be written is skipped and never stops a round.

```sh
queuekeeper queue events                       # everything, oldest first
queuekeeper queue events --follow              # then each new event as it happens
queuekeeper queue events --since 2026-11-12    # from a date or time
queuekeeper queue events --kind entry.         # every kind that begins with entry.
queuekeeper queue events --json                # the journal's own lines
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
| `entry.queued` | A commit range joined the queue | `entry`, `subject`, `commits`, `branch`, `note`, `front`, `noGate`, `why` |
| `entry.landed` | An entry is on `main` | `entry`, `subject`, `landedAs`, `landedTip`, `landedCommits`, `docsOnly`, `ungated` |
| `entry.failed` | An entry came back from its gate | `entry`, `subject`, `why`, `log` |
| `entry.conflict` | An entry came back with a conflict | `entry`, `subject`, `why` |
| `entry.removed` | An entry was taken out | `entry`, `subject` |
| `entries.testing` | Entries went under test | `entries` |
| `entries.requeued` | Entries went back to waiting | `entries` |
| `round.began` | The worker started a round | `round`, `head`, `base`, `entries`, `bisect`, `adopted` |
| `gate.began` | A lane of a gate started | `lane`, `round`, `role`, `head`, `entries` |
| `gate.ended` | A lane of a gate ended | `lane`, `round`, `role`, `status`, `log`, `mac`, `seconds` |
| `batch.passed` | A batch's lanes passed, before its push | `head`, `base`, `entries` |
| `batch.pushed` | A batch was pushed to `main` | `head`, `base`, `entries`, `ungated` |
| `test.flaky` | A failed test passed alone | `test`, `commit`, `issue`, `landed`, `entries` |
| `queue.held` | The queue stopped | `why`, `for` (`owner` or `session`) |
| `queue.resumed` | A hold was cleared | `was` |

A key with nothing to say is left out. The schema only grows: a later version adds kinds and keys
and never renames or removes one, so a reader ignores what it does not know.

The journal starts a new file at 4 MB and keeps the one before it, as `events.1.jsonl`.

The queue's state at one moment, as opposed to its changes, is `queuekeeper queue status --json`.
In Python that is `merge_queue.snapshot(root)`.

## Features

A feature is anything that shows the queue to its owner: a notification, a web page. The rules
that keep one feature's faults and changes to itself:

- **A feature is a module or package of its own** in `queuekeeper.visibility`, found by its name.
  Adding one changes no other file, and deleting its folder removes it.
- **A feature reads only the journal and the snapshot.** The worker never calls a feature, so a
  feature that is slow or broken cannot slow or stop a round.
- **A feature imports the core and never another feature**, and the core imports none of them. A
  test fails a change that breaks this.
- **A feature that is off is never imported**, and adds no command.
- **A feature that fails to load is reported and left out.** The command that was asked for still
  runs.

## Turning features on and off

Every feature is off until its owner turns it on.

```sh
queuekeeper visibility status            # each feature, and whether it is on
queuekeeper visibility enable notify
queuekeeper visibility disable notify
```

What is on belongs to the owner and the Mac, not to a project, so it is kept in
`~/Library/Application Support/queuekeeper/visibility.json` and not in `queuekeeper.json`.

`QUEUEKEEPER_VISIBILITY=off` in the environment turns every feature off for the commands that see
it, whatever the file says.

## The watch feature

`queuekeeper watch` shows the queue in the terminal and draws it again every two seconds until
interrupted: what the queue is doing in one line, each running gate, the entries waiting, the last
few settled and the journal's last events.

```sh
queuekeeper visibility enable watch
queuekeeper watch                  # live, until Ctrl-C
queuekeeper watch --once           # print the view once
queuekeeper watch --interval 5 --events 20
```

Where its output is not a terminal, it prints the view once and exits, as with `--once`.

## Writing a feature

A feature is a module `queuekeeper/visibility/NAME.py`, or a package `queuekeeper/visibility/NAME/`,
with a docstring whose first line says what it does and a `register()` function. `register()` runs
only while the feature is on, and adds the feature's commands as an extension does. To read the
queue, a feature calls `events.read`, `events.follow` and `merge_queue.snapshot`.

## Planned features

- **`notify`**: a process that reads the journal from where it last stopped and tells the owner of
  what needs them: a hold for the owner, an entry that came back, a flaky test, an ungated landing.
  Each channel (a macOS banner, a command given the event on its standard input, a webhook) is a
  file of its own and can be turned off by itself.
- **`dashboard`**: `queuekeeper serve`, a read-only web page served on this Mac alone. Home says
  what the queue is doing and whether it needs its owner; Queue, Farm, Logs and History hold the
  detail. Each tab is a panel in a file of its own, and a header picker switches between
  repositories.

See also [The merge queue](merge_queue.md) and [The agent interface](agent_interface.md).
