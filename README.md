<p align="center">
  <img src="assets/banner.svg" alt="Queuekeeper" width="100%">
</p>

Queuekeeper is a merge queue and build farm for one developer working with AI coding agents. It
runs on that developer's own machines instead of in the cloud. Agents hand it commits; it batches
them, tests each batch once on whichever machine has room, and pushes to `main` only what passed.

What sets it apart is its plugins, which know how to build and test software that needs local tools
and real hardware. The Apple plugin manages simulators, physical devices and Xcode test runs. A
plugin for video game engines is coming soon. Without a plugin, Queuekeeper is one more merge queue
among many.

<p align="center">
  <img src="assets/overview.svg" alt="You direct several AI agents. They send commits to Queuekeeper and get verdicts back. Queuekeeper, made of the merge queue, admission and placement, and plugins, builds and tests on your build farm of Macs, simulators and devices, and pushes what passed to main." width="100%">
</p>

## Why another merge queue?

Several good merge queues exist, and for most teams one of them is the right choice. Queuekeeper
was built for a case they fit poorly:

- **One developer, many agents.** A single developer directing AI agents can produce dozens or even
  hundreds of commits a day. At that rate `main` moves faster than a test run finishes, so the
  changes need a queue. They do not need the pull requests and reviews that team tools are built
  around.
- **Builds that need local tools.** Cloud queues and build farms suit web services and large teams.
  They suit mobile apps, video games and other front-end work less well, because those need tools
  such as Xcode, Unity or Unreal, along with simulators and real devices, which are difficult or
  expensive to run in the cloud.
- **Platform knowledge.** Building and testing for iOS, Android or a game engine unattended takes
  specialized knowledge: managing simulators, sharing a device between runs, driving interactive
  tests, and telling a broken machine from a broken commit. That knowledge belongs in the tool, and
  it is what the plugins are for.
- **Hardware you already own.** The build farm is the machine on your desk plus any others you can
  reach. Nothing has to be rented.

[Related tools](doc/related_tools.md) compares Queuekeeper feature by feature with other merge
queues, including other local queues for AI agents, and says where they are the better choice.

## Getting started

This walk-through adds Queuekeeper to an existing project and lands a first change through it. It
assumes Queuekeeper is installed: [Getting started](doc/getting_started.md) covers the requirements
and installation.

**1. Say how the project is tested.** Create `queuekeeper.json` at the root of the repository. It
needs one lane named `gate`, which is the command every change must pass:

```json
{
  "lanes": {
    "gate": {"command": ["scripts/test.sh"], "timeout": 1800}
  }
}
```

**2. Check the setup.** `queuekeeper doctor` reports anything missing, such as a lane with no
timeout.

```sh
queuekeeper doctor
```

**3. Land the configuration as your first change.** Commit the file on a branch, then queue it
instead of pushing it:

```sh
git switch -c add-queuekeeper
git add queuekeeper.json
git commit -m "Add Queuekeeper"
queuekeeper land --wait
```

Queuekeeper now does what you would otherwise do by hand. It starts a worker for the repository,
applies your commit on top of the latest `main` in a checkout of its own, runs the gate there, and
pushes if the gate passes. Your working tree is not touched. `--wait` follows the commit to its
result and prints the commit it landed as.

**4. Watch the queue.** From any terminal in the repository, see what is waiting, what is being
tested and why anything is held:

```sh
queuekeeper queue status
```

**5. If the change comes back.** When the gate fails or the commit conflicts with `main`,
`queuekeeper land --wait` exits with status 1 and says why, with the path of the log. Nothing was
pushed. Fix the problem, commit, and run `queuekeeper land --wait` again.

**6. Start the next change from `main`.** The queue lands your commits as new commits, so begin
the next piece of work from `origin/main`, not from the old branch.

**7. Hand it to your agents.** Tell your agents to queue their commits with
`queuekeeper land --wait` instead of pushing. Each one's change waits its turn, changes that arrive
together are tested as one batch, and a change that breaks the gate goes back to the agent that
wrote it without blocking the others.

From here, [add a second Mac](doc/getting_started.md#add-a-second-mac) so gates run off your
desk, and turn on the [Apple plugin](#the-apple-plugin) if you build for Apple platforms.

## The Apple plugin

The Apple plugin lets agents build and test iOS and Mac apps without getting in each other's way:

- **Simulator pool.** Each run gets its own simulator, reset from a warmed template in a fraction of
  a second.
- **Test runners.** One build tested on several device types at once, with tests spread across
  simulators.
- **Physical devices.** Agents take an iPhone or iPad in turn, and a run is stopped before its
  reservation ends.
- **Unattended Macs.** Crash dialogs, notification banners, locked keychains and lapsed permissions
  are cleared, or the run is reported as unmeasured instead of failed.

See the [Apple plugin's README](src/python/queuekeeper/apple/README.md) for details.

## Documentation

- [Getting started](doc/getting_started.md)
- [The merge queue](doc/merge_queue.md): landing, batching, flakes, bisecting, holds
- [Lanes and admission](doc/lanes_and_admission.md): what a lane is, and how a machine decides it
  has room
- [The build farm](doc/build_farm.md): running lanes on other Macs
- [The agent interface](doc/agent_interface.md): exit codes and output that agents act on
- [Related tools](doc/related_tools.md)

## Limitations

- **One user so far.** Queuekeeper was extracted from its author's private tooling. Expect rough
  edges, and defaults that suit one person's setup.
- **macOS only**, for the queue and for every build host.
- **One plugin.** Only the Apple plugin exists. The video game plugin is not written yet, and there
  is no Android plugin. The core runs any command as a lane, so those builds can be queued, but
  nothing manages emulators, devices or editors for them.
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
