# The mobile plugin

The mobile plugin is what the [Apple](../../../apple/src/queuekeeper_apple/README.md) and
[Android](../../../android/src/queuekeeper_android/README.md) plugins share: the record that reserves physical
phones and tablets, the bounded runs on them, the reservations a run sent to another Mac carries, the device pass, and
which Mac reaches each device. A project never lists it. Listing either platform's plugin brings it, and it in turn
builds on the [Mac desktop plugin](../../../mac-desktop/src/queuekeeper_mac_desktop/README.md), whose record it adds
devices to.

## Platforms

Each platform plugin registers a `Platform` with the mobile plugin (`platforms.py`). The shared code asks the
platform that owns a device's identifier:

| Question | Apple | Android |
|---|---|---|
| How is a device named? | Hardware UDID, uppercased | adb serial, as adb prints it |
| How is a simulator or emulator named in the record? | Its UUID | `avd:NAME` |
| Is its service up? | CoreDeviceService answers | adb lists it as `device` |
| How are notifications kept out? | The owner's shortcuts; never confirmed | Do Not Disturb over adb, read back |
| How is a run watched for one? | `idevicesyslog`, for a banner | `adb logcat -b events` |
| How does a Mac list what it reaches? | `devicectl list devices` | `adb devices -l` |

Queuekeeper has one device slot, `plugins.DEVICES`, and a slot takes one provider. The mobile plugin provides it
and hands each question to the platform that owns the device, so a project can list both platforms.

## What it adds

- **`qk devices`**: the device record's command line. Use it to reserve, update, take in turn, release and recover
  entries, run a bounded command on a held device (`devices run`), and declare where each device is reached. `take`
  waits in line for any platform's paired keys: `iPhone` and `iPad`, or `AndroidPhone` and `AndroidTablet`.
- **`qk device-pass`**: a release's pass over physical devices, one after another. `{udid}`, `{serial}` and `{key}`
  in its command become the device's identifier and its entry's key.
- **The `devices` and `deviceRunMinutes` lane keys**, with the device windows an admitted run waits in.
- **`qk doctor`** gets a check of the devices the owner declared, using each platform's listing on every Mac.

The behavior is documented in the user guide's [Physical devices](../../../../doc/user_guide.md#physical-devices)
section, which applies to every platform, though its examples name iPhones.

## Shards

`shards.py` packs a run's test classes into shards, using how long each class took last time. Apple's
`qk simulator-tests --shards` and Android's `qk emulator-tests --shards` both use it.
