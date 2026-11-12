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

```sh
queuekeeper land --wait     # queue HEAD and follow it: 0 landed, 1 came back, 75 held or still waiting
queuekeeper queue status    # what is waiting, what is testing, what came back and why
```

Requirements:

- **macOS.** Queuekeeper has not been ported to Linux or Windows.
- **Python 3.14 or newer**, with no other Python packages.
- **Git**, and a repository whose changes land directly on `main` of a remote named `origin`.
- **Optional: more Macs**, each reachable by SSH key, with Python 3.14 or newer.
- **For the Apple plugin:** full Xcode, the same build on every Mac.

Installation, a first `queuekeeper.json` and adding a second Mac are covered in
[Getting started](doc/getting_started.md).

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
