# Getting started

Installation and configuration details for the [setup guide in the README](../README.md#getting-started),
plus optional features to add afterward.

## Requirements

- **macOS.** Queuekeeper reads macOS's load, memory-pressure and screen-lock state and has not been
  ported to Linux or Windows.
- **Python 3.14 or newer.** Queuekeeper needs no other Python packages. Apple's built-in `python3`
  is older, so install a current one, for example with Homebrew.
- **Git**, and a repository that meets two conditions:
  - it has a remote named `origin` that you can push to without a prompt;
  - changes land directly on `main`. Queuekeeper refuses a project that integrates through pull
    requests.
- **A command that tests the project** and exits with a nonzero status when it fails. This becomes
  the gate.

For the Apple plugin, see [its requirements](../plugins/apple/src/queuekeeper_apple/README.md#requirements).

## Install

Clone the repository and run its setup script with the Python that will run Queuekeeper:

```sh
git clone https://github.com/vocaro/queuekeeper.git
cd queuekeeper
python3 tools/install-checkout.py
```

The script puts the `qk` command on your PATH, where it runs this checkout with that Python, and
makes the `queuekeeper` package importable for projects whose own scripts use it. It says so if
the folder it used, `~/.local/bin` unless another on your PATH can be written, is not on your PATH
yet. `python3 tools/install-checkout.py --check` says whether both are in place, and `--remove`
takes them out. Updating Queuekeeper is `git pull` in the checkout.

Check that it runs:

```sh
qk --help
```

## Configure a project

Queuekeeper reads `queuekeeper.json` at the root of each project. The smallest useful file names
one lane, `gate`, which is the command every change must pass:

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

- `schemaVersion` must be `1`, and `name` is your project's nonempty name.
- `extensions: []` starts with the core alone, as does omitting `extensions`. Add the
  [plugins](../README.md#plugins) the project needs explicitly.
- `command` is an argument list, not a shell string.
- `timeout` is in seconds. A lane that reaches it ends as unmeasured, not failed.
- The queue reads its settings from `main`, so a branch cannot loosen its own gate. Only the commit
  that adds `queuekeeper.json` is gated by its own, since `main` has none yet
  ([A project's first landing](../guides/maintenance.md#a-projects-first-landing)).

Then run:

```sh
qk doctor
```

It reports missing commands, lanes without a timeout, and tests that no lane runs.

Two defaults to know about:

- **Preflight lanes.** List the project's cheap checks, such as a linter, in `queue.preflight`,
  and the queue runs them on an entry before it queues it, and first in every round
  ([Preflight lanes](merge_queue.md#preflight-lanes)). Give `"queue": {"preflight": []}` for none.
  Until a project gives it, the queue runs a spelling check of Queuekeeper's, which a later release
  removes.
- **Documentation.** A change that touches only Markdown documents skips the gate and runs the
  documentation lanes instead. Set `"queue": {"docsOnly": false}` to gate every change.

[Lanes and admission](lanes_and_admission.md) describes the other lane settings.

## Land a first change

After reviewing the configuration and agent instructions, ask your agent to commit them on its
branch and run `qk land --wait` in the project. It should follow the entry to its result and confirm
that the setup landed on `origin/main`. The first landing uses the gate in the setup commit;
later landings use the settings on `main`.

## Add a second Mac

Do this once you have landed changes on one Mac.

1. On the second Mac, turn on Remote Login and add the first Mac's public key to
   `~/.ssh/authorized_keys`.
2. On the first Mac, name the second in `~/.ssh/config`, so that `ssh build-host true` succeeds
   without a prompt:

   ```
   Host build-host
       HostName NAME.local
       AddressFamily inet
   ```

3. Install the tools the second Mac needs, then check both Macs:

   ```sh
   qk remote setup
   qk doctor
   ```

From then on, lanes are offered to the second Mac first and run on the first Mac when the second
has no room. [The build farm](build_farm.md) explains how the choice is made and how the code gets
there.

## Keep the queue running

A worker starts by itself when you run `qk land` and exits when nothing is waiting. To
have the queue also check for waiting work at login and every two minutes, set up the signed
launcher. It also avoids a macOS restriction that can cut a background worker off from the local
network. See [The build farm](build_farm.md#macos-local-network-privacy).

## See what the queue is doing

`qk queue status` says what is waiting and why. Three features, each off until you turn
it on, show more:

```sh
qk visibility enable watch
qk watch                          # the queue in the terminal, redrawn as it changes

qk visibility enable notify       # a banner when the queue needs you
qk notify test

qk visibility enable dashboard
qk serve --open                   # a read-only web page of the queue
```

[Visibility](visibility.md) describes each one.

## Tell your agents

Agents need to know to queue their work instead of pushing it. Add a rule like this to the
instructions your agents read, such as `AGENTS.md` or `CLAUDE.md`:

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

[The agent interface](agent_interface.md) lists the exit codes and output agents can rely on, how
to verify a change, and what to do after an interrupted wait.
