# The Android plugin

The Android plugin lets Queuekeeper build and test Android apps unattended. It provides an emulator pool, a runner
that builds an app's instrumented tests once and runs them on several emulators at the same time, and reservations
for physical Android phones and tablets.

It is optional, and the core runs without it. It builds on the [mobile plugin](../../../mobile/src/queuekeeper_mobile/README.md),
which it shares with the [Apple plugin](../../../apple/src/queuekeeper_apple/README.md). A project may list both.
It was developed against the Android SDK's command-line tools, emulator 36 and Android 16 (API 36) arm64 images on
an Apple silicon Mac. Its physical-device paths were written without an Android phone to try them on and are not yet
verified on hardware.

## Setting it up

```json
"plugins": ["queuekeeper_android"],
"emulatorPool": "Pagepour",
"android": {"directory": "android", "module": "app", "variant": "debug"}
```

`android.directory` is the Gradle project's folder, the one with `gradlew`. If it's left out, the checkout's root is
used. `module` is the application module, and `variant` its build variant. `gradleArguments` adds words to every
Gradle command.

## Emulator pool

A script that starts an emulator under a fixed AVD name, or relies on adb's single-device default, ends up on an
emulator another agent is using. The pool gives each run its own.

- **Taking an emulator.** `qk emulator-pool take --device pixel_8 --purpose "reader tests"` reserves a free member of
  that kind, or creates one. It resets the member, starts it headless on a console port of its own, waits for the
  boot to finish, and prints its AVD and adb serial. Every command names that serial with `adb -s`.
- **Warmed templates.** A new image's first boot takes minutes. The pool boots one template per kind once, turns
  off the window, transition and animator animations, as instrumented tests expect, and shuts it down. Each member
  is an APFS copy-on-write clone of the template, so it boots in seconds and starts as the template left it.
- **Releasing.** `qk emulator-pool release --serial emulator-5556 --outcome "..."` stops the emulator, confirms adb
  no longer lists it, and releases the reservation. An emulator that does not stop keeps its reservation for
  recovery.
- **Reaping an abandoned member.** `qk emulator-pool reap --dry-run` checks this Mac's named pool. Run it without
  `--dry-run` to stop and release an emulator at least two hours old when its reservation has expired, its recorded
  holder processes have ended and no process still names its AVD, serial or owner. The reaper checks the recorded
  process owns the console connection, asks that console for its AVD name, and sends the stop down the same connection.
  The reservation stays held until the recorded process, emulator ports and adb listing all confirm it stopped.
  `qk emulator-pool reap --farm --dry-run` runs the same checks on this Mac and every active farm Mac for that pool.
  A Mac needs a Queuekeeper checkout with this command at `~/Development/queuekeeper`. Unchecked Macs are named in
  the report and make the command exit unsuccessfully. A Mac whose AVD folder cannot be read, or is missing while
  its record holds an unreleased member, is unchecked; only a Mac whose inventory was read reports no members. To act on a report, run `qk emulator-pool reap` on the Mac
  that owns the emulator, where it checks ownership again before attempting a stop.

Members are AVDs named `POOL_LABEL_N`, such as `Pagepour_Pixel_8_API_36_2`. Each is keyed by that name in the
[device record](../../../../doc/user_guide.md#taking-a-device). Each Mac has its own pool.
`qk emulator-pool list` names the emulators that adb can identify. If adb is unavailable or a listed
emulator does not identify itself, other members show an unknown emulator state. A member absent from
a complete listing is reported as `not listed by adb`.

From an Android-enabled checkout, `qk disk` measures the host's known pool members and templates, emulator test
logs in the main project checkouts, and Gradle's caches and wrapper distributions. Pool rows show the recorded
reservation or the time since release when available. These items are kept by `qk disk --delete`.

### Deleting unused members

The pool has a retention policy, which is off by default:

- Delete a pool member that has not been used for 14 days.
- Never delete the pool's last template.
- Delete only on a Mac whose AVD inventory reads as verified.
- Record every deletion.

While the policy is off, `qk emulator-pool reap` and its `--dry-run` work as before. They also list each member
the policy would delete, with its last-use evidence and the note `emulatorRetention.deleteUnused is off`. A member
whose last use cannot be shown reliably is listed as kept. The farm dry run, `reap --farm --dry-run`, reports the
same for every active farm Mac.

Turn it on only after the farm dry run has shown reliable last-use times on every farm Mac
([#245](https://github.com/vocaro/queuekeeper/issues/245)). To turn the policy on, add this key to the project's
`queuekeeper.json`:

```json
"emulatorRetention": {"deleteUnused": true, "unusedDays": 14}
```

`unusedDays` is 14 if it's left out. Each `qk emulator-pool reap` run without `--dry-run` then applies the policy
on the Mac it runs on. A member is deleted only if all of these hold:

- Its reservation is released. No other reservation names its AVD, and its AVD folder and pointer are its own.
- Its last use is more than `unusedDays` ago. Last use is the later of the recorded release and the newest
  change to its files. If the files changed more than five minutes after the release, the member was used outside
  the record, so it is kept. A missing or unreadable release time also keeps it, as does a release earlier than
  its reservation.
- adb's listing is complete and doesn't show its emulator. Its recorded emulator process and ports are shown
  stopped, and no process names it.

A template is deleted the same way. Its own use and every member's use count toward its last use, and no member
of its kind may remain. If every template in the pool would go, the most recently used one is kept. Each
deletion takes a reservation at the revision its evidence was read at, so a member taken in the meantime is
kept. It checks the emulator again and appends a `deleting` line to `~/.android/queuekeeper-emulator-deletions.jsonl`
before removing anything. That line holds the AVD, its path, the reason and its last-use evidence. A `deleted`
line follows once both the folder and the pointer are confirmed gone. If the line cannot be written, nothing is
deleted. If a deletion cannot be confirmed, its reservation is kept for recovery and the command exits 1.

`qk emulator-pool prune --avd NAME --dry-run` checks one local member without removing any files. The check keeps
templates, the lowest-numbered member of a kind, and members whose last reservation kept state or was released
within an hour. It requires a verifiable AVD directory, reservation, and local liveness readings. Passing the
check describes the available evidence at that moment. Android does not supply the simulator pool's separate
last-use reading, and this command only reports the time since the last Queuekeeper release.

## Emulator tests

```sh
qk emulator-tests --device pixel_8 --device pixel_tablet
```

This builds the app and its test APK once with the project's Gradle wrapper. Each device kind gets its own pool
emulator, and the kinds run at the same time. For each kind it installs both APKs and runs `am instrument -r -w`,
reading each test's outcome as it streams.

- `--shards N` spreads one kind's test classes across N emulators. Shards are packed by how long each class took in
  the last run, the same way `qk simulator-tests --shards` packs them.
- `--repeat N` runs everything N times on fresh emulators from the one build.
- `--class CLASS[#METHOD]` limits the run to particular tests, and `--argument KEY=VALUE` passes an instrumentation
  argument.
- Each kind's outcome is reported on its own line as `Executed N tests, with K tests skipped`, which a lane counts.
  The run fails if any kind failed. It is unmeasured if any kind was: an emulator that did not boot, an
  instrumentation that crashed mid-test, or a run in which no test ran.

Each kind's per-test outcomes and seconds are kept in the lane's `results/emulator-tests/KIND.json`.
`qk run` prints them when the lane finishes, after bringing the results back if it ran on another Mac.

## Physical devices

An Android phone is reserved in the same record as an iPhone, under its adb serial, and goes through the same
`qk devices` and `qk device-pass` commands. The first reservation records the device:

```sh
qk devices reserve AndroidPhone --owner S --revision 0 --purpose "..." --release-by T --device 33041FDH2000QK
qk devices run AndroidPhone -- env ANDROID_SERIAL=33041FDH2000QK ./gradlew connectedDebugAndroidTest
```

Gradle's `connectedAndroidTest` runs on every device adb lists, so the command names its device with
`ANDROID_SERIAL`. A command run under a reservation that names another device's serial is refused. A device's serial is case-sensitive,
and the record keeps it exactly as adb prints it.

### Notifications

A notification that arrives mid-run moves TalkBack's focus and puts private text on screen. Unlike an iPhone, an
Android phone needs no setup by the owner:

- Reserving or taking a device turns on Do Not Disturb (priority only) with `adb shell cmd notification set_dnd`
  and reads `zen_mode` back to confirm it. Releasing the device restores the mode it found. A device already in Do
  Not Disturb is left as it is.
- `devices run` watches the device's event log (`adb logcat -b events`) during the run. A notification from another
  app, or one that sounded or vibrated, makes the run unmeasured, and the run is repeated once.

Which event tags a given vendor's build logs for a heads-up banner is not yet verified on hardware.

### TalkBack

`talkback.py` reads and restores TalkBack's two secure settings. A run that turns TalkBack on can use
`talkback.preserved(serial)` to put it back afterward.

## Requirements

- The Android SDK command-line tools, platform-tools, the emulator, and a system image:

  ```sh
  brew install --cask temurin@21 android-commandlinetools
  sdkmanager --install "platform-tools" "emulator" "platforms;android-36" "build-tools;36.0.0" \
      "system-images;android-36;google_apis;arm64-v8a"
  ```

- A JDK 17 or later for Gradle and `avdmanager`. Queuekeeper finds the SDK through `ANDROID_HOME`, then
  `ANDROID_SDK_ROOT`, then `~/Library/Android/sdk` and Homebrew's location. `qk doctor` says what is missing.
- On a farm, `qk doctor` compares the installed revisions of platform-tools, the emulator and system images.
  Differences are errors. A Mac whose SDK inventory could not be read is listed under `unchecked`. Use
  `sdkmanager --list_installed` on the Macs to check which revisions need to be aligned.
