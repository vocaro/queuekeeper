# The Apple plugin

The Apple plugin gives Queuekeeper what it needs to build and test iOS and Mac apps unattended:
simulator pools, physical-device reservations, Xcode test runners, and guards against the dialogs
and permissions that stop a test run on a Mac nobody is watching.

It is optional, and the core runs without it. It builds on the
[mobile plugin](../../../mobile/src/queuekeeper_mobile/README.md), which holds the device record, bounded runs and
the device pass it shares with the [Android plugin](../../../android/src/queuekeeper_android/README.md), and on the
[Mac desktop plugin](../../../mac-desktop/src/queuekeeper_mac_desktop/README.md). A project that
lists the Apple plugin gets both. The plugin was developed against Xcode 27.2, iOS 27 simulators
and macOS 27, and parts of it depend on behavior Apple does not document, which a new release can
break.

## Simulator pool

Scripts that pick a simulator by name, or use `booted`, end up on Xcode's default simulators or on
one another agent is using. The pool gives each run its own.

- **Taking a simulator.** `qk simulator-pool take --device iPhone-17-Pro --runtime iOS-27-0`
  reserves a free member of that kind, or creates one, and prints its identifier. Reservations go
  through one locked record per Mac, so two agents cannot take the same simulator. `check` refuses
  any simulator outside the pool.
- **Warmed templates.** The pool warms one template per kind and resets each member by copying the
  template's data with APFS clones. In earlier measurements, the first system voice query in an
  Xcode clone of a freshly erased simulator took 285 to 344 seconds on a quiet Mac and far longer
  under load. Clones of a warmed template answered in 11 to 13 seconds. In a November 2026
  measurement, cloning a 2.2 GB template on APFS took 0.24 seconds.
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

## Reused build folders

A worker checkout keeps its build folders between gates, so a lane's `xcodebuild` or `swift` called by
name runs through a wrapper that recovers from two faults of a reused build folder, once each:

- **Stale explicit modules.** A build that fails with `has been modified since the module file` has the
  module cache it names removed and runs once more, saying `STALE MODULES`.
- **A replaced Metal Toolchain mount.** macOS mounts the Metal Toolchain from a cryptex under
  `/var/run/com.apple.security.cryptexd/mnt/`, and can mount it again at a new path. A build plan made
  before keeps the old one, and every `.metal` file then fails with `unable to spawn process
  '.../com.apple.MobileAsset.MetalToolchain-.../metal' (No such file or directory)`, although the
  toolchain is installed. The wrapper removes each `Intermediates.noindex/XCBuildData` inside the
  checkout that names the old mount, and runs once more, saying `STALE TOOLCHAIN`. If no plan inside the
  checkout names it, or the rerun fails the same way, the lane ends unmeasured, not failed: the fault is
  the Mac's, not the change's, and the message says what to check.

## Physical devices

An iPhone can run one test session at a time, and nothing in Xcode stops two agents from installing
to it at once.

- **Reservations.** Each Mac keeps a record of who holds each device and until when. Agents take a
  device in turn, first come, first served.
- **Bounded runs.** A command run under a reservation is stopped before the reservation ends, with
  time left to restore the device. A command that names a different device is refused.
- **Across Macs.** A device paired with two Macs is reserved in one record, and the reservation
  travels with a run sent to another Mac.
- **Notifications.** A banner that arrives mid-run moves VoiceOver's focus and puts private text on
  screen. The plugin runs a Shortcut on the device to silence notifications, watches the device log
  for banners, and marks a run that one reached as unmeasured.

The record coordinates only work that goes through Queuekeeper. A person running `xcodebuild` by
hand bypasses it. It currently tracks one iPhone and one iPad.

## Kept and probe app builds

`qk app` keeps the last good build of a project's app and runs it: on this Mac, or with `--iphone`
or `--ipad` installed and launched on that device as a device pass. The pass takes the device in
turn, silences its notifications, records the launched app, and releases the device afterward.

A diagnostic sometimes needs a build that never lands, such as the app with one more logging line.
`qk app build` builds the working tree of the checkout it runs from, as it stands. It runs the
project's `prepare` steps and settings in the environment `qk` gives its lanes, admitted on this Mac,
and prints the `.app`'s path. `qk app run --ipad --app PATH` runs such an `.app` under the same device
pass as a kept build, and `qk app run --ipad --here` builds and runs one in a single command. A given
`.app` is refused unless it is a device build, not a Simulator or Mac build, and its bundle identifier
is the project's. Probe builds are never kept, and `qk disk` offers each checkout's for deletion once
they are idle.

## Unattended Macs

A UI test on a Mac nobody is watching fails for reasons unrelated to the code. The
[Mac desktop plugin](../../../mac-desktop/src/queuekeeper_mac_desktop/README.md) handles failures
outside Xcode. It reserves the Mac's foreground for one run at a time, clears crash alerts and
notification banners, keeps the login keychain unlocked, and tracks screen-capture approvals.

The Apple plugin adds two of its own:

- **App registrations.** Every built copy of a Mac app stays registered, and the system can launch
  an old one, so the plugin unregisters the copies a run built.
- **VoiceOver.** A run can leave VoiceOver on, or toggle it late. The plugin saves its state when a
  run takes the foreground and restores it before the foreground is released.

## Keeping Macs alike

Test results from two Macs are comparable only if the tools match. `qk doctor` compares the
Xcode build and the installed simulator runtimes on every Mac. It evens out the runtimes by copying
a runtime image to the Mac that lacks it, or deleting one that nothing uses. `qk disk`
lists stale DerivedData folders, SwiftPM caches, unused runtimes, superseded and beta device
support folders and large caches of macOS tools, and deletes them on request.
Neither deletes a runtime while any simulator on that Mac is running, since the deletion can crash
the launch host that every simulator shares.

## Requirements

- Full Xcode, not only the Command Line Tools, and the same build on every Mac.
- For watching a device's notifications during a run: libimobiledevice.

See [The build farm](../../../../doc/user_guide.md#the-build-farm) for how work reaches another Mac.
