# Getting started

What to install and set up before the [walk-through in the README](../README.md#getting-started),
and what to add afterward.

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

For the Apple plugin, see [its requirements](../src/python/queuekeeper/apple/README.md#requirements).

## Install

Clone the repository and install the `queuekeeper` command from the checkout. `qk` is installed as
a short alias.

```sh
git clone https://github.com/vocaro/queuekeeper.git
cd queuekeeper
uv tool install --editable .
```

Check that it runs:

```sh
queuekeeper --help
```

## Configure a project

Queuekeeper reads `queuekeeper.json` at the root of each project. The smallest useful file names
one lane, `gate`, which is the command every change must pass:

```json
{
  "lanes": {
    "gate": {"command": ["scripts/test.sh"], "timeout": 1800}
  }
}
```

- `command` is an argument list, not a shell string.
- `timeout` is in seconds. A lane that reaches it ends as unmeasured, not failed.
- The queue reads its settings from `main`, so a branch cannot loosen its own gate. The one
  exception is the first landing, which may bring `queuekeeper.json` itself.

Then run:

```sh
queuekeeper doctor
```

It reports missing commands, lanes without a timeout, and tests that no lane runs.

Two defaults to know about:

- **Spelling.** Before the gate, the queue runs a spelling check that flags British spellings. Turn
  it off with `"queue": {"spelling": false}`, or define your own lane named `spelling`.
- **Documentation.** A change that touches only Markdown documents skips the gate and runs the
  documentation lanes instead. Set `"queue": {"docsOnly": false}` to gate every change.

[Lanes and admission](lanes_and_admission.md) describes the other lane settings.

## Land a first change

Follow the [walk-through in the README](../README.md#getting-started).

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
   queuekeeper remote setup
   queuekeeper doctor
   ```

From then on, lanes are offered to the second Mac first and run on the first Mac when the second
has no room. [The build farm](build_farm.md) explains how the choice is made and how the code gets
there.

## Keep the queue running

A worker starts by itself when you run `queuekeeper land` and exits when nothing is waiting. To
have the queue also check for waiting work at login and every two minutes, set up the signed
launcher. It also avoids a macOS restriction that can cut a background worker off from the local
network. See [The build farm](build_farm.md#macos-local-network-privacy).

## Tell your agents

Agents need to know to queue their work instead of pushing it. Add a rule like this to the
instructions your agents read, such as `AGENTS.md` or `CLAUDE.md`:

```markdown
Never push to main. Commit on your own branch, then run `queuekeeper land --wait` as a
background command and follow it to its result. Exit 0 means landed, 1 means it came back
with a reason, and 75 means the queue is held or still waiting.
```

[The agent interface](agent_interface.md) lists the exit codes and output agents can rely on.
