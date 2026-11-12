# The Apple plugin

The Apple plugin gives Queuekeeper what it needs to build and test iOS and Mac apps unattended:
simulator pools, physical-device reservations, Xcode test runners, and guards against the dialogs
and permissions that stop a test run on a Mac nobody is watching.

It is optional, and the core runs without it. It was developed against Xcode 27.2, iOS 27 simulators
and macOS 27, and parts of it depend on behavior Apple does not document, which a new release can
break.

## Simulator pool

Scripts that pick a simulator by name, or use `booted`, end up on Xcode's default simulators or on
one another agent is using. The pool gives each run its own.

- **Taking a simulator.** `qk simulator-pool take --device iPhone-17-Pro --runtime iOS-27-0`
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

## Simulator tests

```sh
qk simulator-tests --scheme App --runtime iOS-27-0 \
    --device iPhone-17-Pro --device iPad-Air-13-inch-M4
```

This builds once and tests on each device type at the same time, each on its own pool simulator,
with its own log and result bundle.

- `--shards N` spreads one device's tests over N simulators, packed by how long each class took in
  the last run. Xcode's own parallel workers do not spread Swift Testing suites this way.
- `--repeat N` builds once and runs N times on fresh simulators, to show that a fix for an
  intermittent failure holds.
- Each device's outcome is reported separately. The run fails if any device failed, and is
  unmeasured if any device was.

It covers iOS simulators only, one runtime per run.

## Swift package tests

`swift test --parallel` reports a skipped XCTest test as passed and prints no total.
`qk xctest` builds once, packs XCTest classes into shards by their last run times, runs the
shards side by side and prints one line with the counts of tests run and skipped. A shard that ran a
different number of tests than it was given counts as failed.

For lanes that call `xcodebuild` themselves, `qk results keep` reads a result bundle and
records each test's outcome and duration in the same form.

## Physical devices

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

## Unattended Macs

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

## Keeping Macs alike

Test results from two Macs are comparable only if the tools match. `qk doctor` compares the
Xcode build and the installed simulator runtimes on every Mac. It evens out the runtimes by copying
a runtime image to the Mac that lacks it, or deleting one that nothing uses. `qk disk`
lists stale DerivedData folders, SwiftPM caches and unused runtimes, and deletes them on request.

## Requirements

- Full Xcode, not only the Command Line Tools, and the same build on every Mac.
- For watching a device's notifications during a run: libimobiledevice.

See [The build farm](../../../../doc/build_farm.md) for how work reaches another Mac.
