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
"extensions": ["queuekeeper_android"],
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

Members are AVDs named `POOL_LABEL_N`, such as `Pagepour_Pixel_8_API_36_2`. Each is keyed by that name in the
[device record](../../../../doc/user_guide.md#taking-a-device). Each Mac has its own pool.

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
