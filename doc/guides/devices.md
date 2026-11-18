# Physical devices

What the Apple plugin does around a run on a paired iPhone or iPad: silencing the owner's notifications,
the device pass that runs a project's device checks under one reservation, and the owner's declaration of
each device with what each Mac reads of it. Reservations themselves are
in [shared host](shared-host.md#device-reservations); how devices behave under test is in the toolkit's
[device testing](https://github.com/vocaro/apple-engineering/blob/main/guides/device-testing.md) guide.

## Notifications during a run

On 2026-11-04, in an approved iPhone window, VoiceOver read one of the owner's notifications in
the middle of a session, which was discarded and run again (vocaro-engineering#144). The iPhone
is his personal phone and the iPad gets his notifications too, so a run silences them for itself
and puts back what was there before, as foreground runs do with the Mac's crash-report dialogs.

What the Mac can do, with Xcode 27.1 (`devicectl` 651.32), November 2026, from `--help` and
Apple's documentation, not yet observed on a device:

- Nothing sets or reads a device's Focus from the Mac. `devicectl device settings` covers
  appearance, audio, biometrics, StandBy and VoiceOver, and `device info` reads no Focus.
  `device notification post|observe` handles Darwin notifications, not the user's.
- The configuration-profile payload for notification settings
  (`com.apple.notificationsettings`) needs a supervised device; the owner's are not, and it
  changes apps' settings rather than turning on a Focus.
- `devicectl device process launch --payload-url URL com.apple.shortcuts` opens Shortcuts with
  a URL, and `shortcuts://run-shortcut?name=NAME` runs a shortcut by name (Shortcuts User Guide,
  Run a shortcut from a URL). So Queuekeeper runs two shortcuts the owner makes once on each device.
  Nothing returns their result to the Mac, so the silencing is never confirmed: Queuekeeper records that
  it asked and how `devicectl` answered.

### The owner's one-time setup

On each device, in Shortcuts, the owner makes three shortcuts with exactly these names:

1. **Vocaro Silence Notifications**: Get Current Focus; If Current Focus does not have any
   value: Set Focus, turn Do Not Disturb on until turned off, then Text `eng` and Save File to
   the Shortcuts folder with Subpath `silenced.txt` (Ask Where to Save off, Overwrite If File
   Exists on); End If; Go to Home Screen.
2. **Vocaro Restore Notifications**: Get File `silenced.txt` from the Shortcuts folder (Error
   If Not Found off); Get Text from Input, the File; If Text is `eng`: Set Focus, turn Do Not
   Disturb off, then Text `off` and Save File as in the first, its input that `off` Text; End
   If; Go to Home Screen.
3. **Vocaro Test Notification**, for the window below: Show Notification "Vocaro test".

The marker file is what puts back the state from before: a Focus the owner already had on is
left alone and is not turned off afterwards, and running either shortcut twice changes nothing.
Restore overwrites it with `off` rather than deleting it: on iOS 27 the Delete action asked
"Allow … to delete 1 file?" with only Cancel and Delete at every run, which no unattended run
can answer, while saving asks once and offers Always Allow (2026-11-05, on the iPhone). Where
an action takes a value, pick the earlier action's output with Select Variable (touch and hold
the field); Shortcut Input, which a launch from the Mac leaves empty, is the wrong one, and of
Restore's two Text outputs the one to save is the `off` Text's.

Run on a Mac with `shortcuts run`, both shortcuts end with "The action “Go to Home Screen” is not supported
on Mac" and exit 1, after their Focus step has run. On 2026-11-07 Restore, run on this Mac with the marker
set to `eng`, turned the owner's shared Do Not Disturb off. On another Mac, which has no iCloud Drive,
Silence turned Do Not Disturb on and then failed at Save File, so Restore cannot undo it there.

He runs each once by hand, starting with no Focus on, and answers each prompt with Always
Allow: Silence, then Restore (Do Not Disturb goes off), then Restore again (nothing changes).
The marker is in iCloud Drive's Shortcuts folder, so the Mac can read it at
`~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/silenced.txt`: `eng`
after Silence, `off` after Restore. In Settings ▸ Focus ▸ Do Not Disturb he turns off
Intelligent Breakthrough & Silencing, allows no people and no apps, sets Allow Calls From to
Allowed People Only, and turns off Time Sensitive notifications and repeated calls; contacts
with Emergency Bypass still ring. With Share Across Devices on, Do Not Disturb follows to his
other devices, the Macs included, for the run.

Then a session that holds the device records his word, once per device:

```sh
qk devices notifications setup iPad --by owner --at 2026-11-04T09:00:00Z \
  --words "the shortcuts are on the iPad"
```

The setup names the device the entry records, so a replaced device needs it again.

### What a run does

- `devices reserve` and `devices take` of a physical device run **Vocaro Silence
  Notifications** right after reserving it, and the entry's `notifications` records each step:
  `restore: pending` before the shortcut runs, so that a run that dies after it still puts the
  notifications back, then `silence: requested` with `confirmed: false`, or `failed` with
  `devicectl`'s answer. Shortcuts is recorded among the entry's launched apps.
- Without the owner's recorded setup, nothing runs and the entry records nothing. The command
  says what to ask the owner, in the same message as the window, so it is not a second
  interruption mid-run: to turn on Do Not Disturb himself and say when it is on. Record that
  with `devices notifications by-owner KEY --owner SESSION --revision N --by owner --at TIME`;
  he turns it off himself afterwards.
- A silencing that fails, for example on a locked device, which refuses the launch, is said
  and kept pending for restoration. Try again once the device is unlocked with
  `devices notifications silence KEY --owner SESSION --revision N`.
- Every silencing records `restore: pending` before its shortcut runs, a silencing after
  `devices notifications restore` in the same hold too, so the release or recovery runs the
  restoring shortcut again. Asking again while a silencing is still pending runs nothing.
- `devices run` says before it starts when the entry's notifications are not silenced, and
  records the state in the run.
- A silencing can take a moment to hold. On 2026-11-07 a banner reached the iPhone within
  seconds of Silence running, during `qk app run --iphone`. The watch below counted it, and the pass ran the
  install and launch once more, which saw none (vocaro-engineering#206).
- `devices run` also watches the device's own log while its command runs, for a notification
  that gets through anyway. `idevicesyslog` relays SpringBoard's lines over USB and over the
  network at once, into a temporary directory only this account can read. Over the network it
  needs lockdown's `com.apple.mobile.wireless_lockdown` `EnableWifiConnections` on the device.
  `devicectl` reaching a device over Wi-Fi (`Transport Type: localNetwork`) is Xcode's
  CoreDevice tunnel, which libimobiledevice cannot use, and Finder's "Show this iPhone when on
  Wi-Fi" left the key `false` (2026-11-05). No libimobiledevice command sets it: it was set over
  USB by a few lines calling `lockdownd_set_value`, with the owner's approval for each device,
  since it lets every Mac paired with the device reach its lockdown services over the LAN
  (vocaro-engineering#194). On 2026-11-06, on iOS 27.2:
  the owner's iPhone has it on. Unplugged, `idevice_id -n` lists it, and its log reaches this Mac
  over Wi-Fi alone: the relay connected in 0.42 s and counted the test banner once. After a
  restart it was listed again only once the owner unlocked it (84 s), and the first relay then
  lost it and ended, which records the run as not watched; the next ones counted the banner. No
  cable is needed for a watched run on the iPhone. The iPad was never listed over the network,
  plugged in, unplugged or after a restart, though it advertised `_apple-mobdev2._tcp` and its
  pairing record held its Wi-Fi address, so its key was turned back off: plug the iPad in for a
  watched run. Without a relay that reached the device, the run is recorded as not watched. When
  the command
  ends, Queuekeeper counts the banners SpringBoard presented, then deletes the lines. The run's
  `notificationWatch` keeps only that count, the device's clock at the first banner and the
  `transport` it was watched over (`usb`, `network` or `usb+network`); what a notification said
  is the owner's and is never printed or kept.
- A run is watched only through a relay that reached the device before the command started
  (its `[connected:UDID]` or a log line; Queuekeeper waits up to 3 s for each relay still running, not
  only the first), relayed its log, and was still relaying it when the command ended. A relay that lost the device (`[disconnected:UDID]`, or ended: Queuekeeper
  starts it with `-x`, which ends it when the device goes; the `[disconnected:UDID]` it prints when
  Queuekeeper ends it is read as neither), or that reached the device only after
  the run had started, read only part of the run, so a banner in the gap would go unseen. With no
  other relay that read the whole run, the run is recorded as not watched, naming the relay and
  why, never as watched with no banner (vocaro-engineering#194). A banner a partial relay did see
  still makes the run unmeasured.
- A run that a banner reached is unmeasured: its transcript may hold the notification, and
  VoiceOver's cursor may have moved. `devices run` says so and runs the command once more, and
  the rerun's status is the run's, with the first run kept as `rerunOf` and `measured: false`.
  If a banner reaches the rerun too, or no time is left for one, the run exits 3 (unmeasured)
  and is not run a third time: ask the owner to check Do Not Disturb on the device. A run whose
  log could not be read, for example without `idevicesyslog` (`brew install libimobiledevice`),
  says so and keeps its status; read its transcript for a notification as before, and discard
  a session that picks one up unread.
- A run sent with `qk remote run|admit --device KEY` is watched the same way, and sent once
  more after a banner, offering the commits it sent the first time (vocaro-engineering#192).
  The relay runs on this Mac, the one that sends the run, for the whole dispatch, and watches
  every `--device`'s log; a banner on any of them makes the run unmeasured. Each entry's `run`
  keeps the `notificationWatch`, and `rerunOf` as above. The relay runs here, though the second
  Mac reaches a paired device too, because this Mac's record holds the reservation, and this
  Mac silences, restores and releases the device, so it reaches the device already; the relayed
  lines never leave the Mac that read them, and only the count and the first banner's clock go
  in its record; and `offload_host.py`, which runs on another Mac before the toolkit is
  checked out there, imports none of it, so a relay there would be a copy of the watch. A device
  without Wi-Fi lockdown (the iPad) is plugged into this Mac for a watched leased run: plugged
  only into another Mac, it is recorded as not watched, with the reason. The iPhone's log
  reaches this Mac over the network (vocaro-engineering#194). The watch spans the dispatch, the wait for admission there
  included, so a banner before the command starts also sends it again: a spare run, never a
  missed banner.
- `devices release` runs **Vocaro Restore Notifications** first, through the CLI, and records
  `restore: done` or `failed`; a failed restore is said, for the owner, and the release goes on.
  `Record.release` from a script refuses while a restore is pending.
  `devices recover`, after a run whose process died, runs it too, once recovery would go ahead.
  `status` shows each held device's notifications and a released one left unrestored.
- The shortcut finishes after `devicectl` returns, and the Mac cannot tell when. On the
  owner's iPhone (iOS 27.2, 2026-11-05) a shortcut opened by URL ran without asking once its
  first-run prompts had been answered by hand, Get Current Focus worked, and each shortcut
  wrote its marker about 1 s after `devicectl` returned. The marker, in iCloud Drive's
  Shortcuts folder, is how the Mac confirms a shortcut ran; the iPad, whose Shortcuts the owner
  does not sync, has no recorded setup.

### Verifying on a device

The iPhone was verified on 2026-11-05 (vocaro-engineering#144): Silence and Restore from the
Mac, and a Focus the owner set left on. What that window taught:

- **A notification from the app on screen is not a Focus test.** Shortcuts' Show Notification,
  run while `devicectl` holds Shortcuts in front, showed its banner with Do Not Disturb on: iOS
  presents an app's own notifications while it is frontmost. Post the test notification from
  an app in the background. A reminder made on the Mac served, through iCloud:
  `osascript -e 'tell application "Reminders" to make new reminder with properties {name:"Vocaro test", remind me date:(current date) + 90}'`
  (macOS asks once for Automation control of Reminders). Delete it afterwards by the id the
  command returns; a `whose` query scans every reminder and took over a minute.
- **SpringBoard says which way a banner went**, in lines that hold identifiers, not content.
  Shown: `Destination BulletinDestinationBanner presentable did appear as banner:
  <SBNotificationPresentableViewController …>`. Silenced by a Focus: `Banner for notification
  B255-16C4 was dropped. suppressed: 1 application: 0, do not disturb: 1 …`, after
  `Resolved event behavior=… interruptionSuppression: delay delivery`. Both follow `Posting
  notification ID to destination SBNotificationBannerDestination`, so that line alone does not
  mean a banner showed. `device_notifications.PRESENTED`, written before any real line was
  seen, matched neither, and `LINE` expected a device name before the process that these lines
  do not have, so the watch would have called a log it read unwatched. Both now follow the
  recorded lines in `tests/apple/fixtures/DeviceLog`.

The first approved window on another device checks, with the owner's shortcuts made and no
Focus on:

1. Run **Vocaro Silence Notifications** through `devices notifications silence`, and have the
   owner say whether iOS asked before running it, whether Do Not Disturb came on, and roughly
   how many seconds that took after `devicectl` returned.
2. With Do Not Disturb off and then on, post a test notification that says nothing of the
   owner's from an app in the background, such as the "Vocaro test" reminder above, while
   `idevicesyslog -u UDID --no-colors -p SpringBoard` runs. Keep only SpringBoard's lines about
   that notification, check that `device_notifications.presentations` finds the banner with Do
   Not Disturb off and nothing with it on, and add the lines to `tests/apple/fixtures/DeviceLog` if
   they differ from the iPhone's.
3. Run **Vocaro Restore Notifications** through `devices release`, and have the owner confirm
   that Do Not Disturb is off and that the marker reads `off`. Repeat it with a Focus he had
   already turned on, which must stay on.

## The device pass

A project's physical-device checks, such as its device VoiceOver sessions, run as one release
lane whose command is `qk device-pass`
([#148](https://github.com/vocaro/vocaro-engineering/issues/148)). `qk run` admits it and writes
its receipt, so `eng release` counts a pass of it at the commit as it counts any release lane's.

```json
"device-voiceover": {
  "command": ["python3", "scripts/qk", "device-pass", "--device", "iPad", "--device", "iPhone",
              "--", "python3", "scripts/run-device-voiceover.py", "--device", "{udid}"],
  "resource": "heavy", "here": "physical devices, reserved in this Mac's device record",
  "timeout": 7200, "minimumTests": 2
}
```

- The lane is heavy and says `"here"`: physical devices are reserved in this Mac's record only,
  and another Mac's defers them, so a pass sent there refuses. `qk doctor` warns of a device
  pass's lane without `"here"`, and of a `gate` that runs one.
- For each device named, in the order named, the pass uses the session's own reservation when it
  holds one, and leaves it held; or else takes the device in turn, first come first served, with
  the owner's notifications silenced, and releases it afterward, putting them back. A device
  whose turn does not come within `--wait` is not run. The pass checks no approval (owner's
  decision, 2026-11-06): run a lane that names the iPhone only once the owner has agreed.
- A lane on a device the session already holds waits until the latest start that leaves room for
  its estimated runtime and the reservation's termination and restoration allowance. If that
  start passes, it returns BUSY and records no device result
  ([#292](https://github.com/vocaro/vocaro-engineering/issues/292)). Declare `deviceRunMinutes`
  from the build, every device's sessions, and cleanup inside the lane. Queuekeeper uses the larger of
  that budget and the median of its five latest whole, passing runs for the same device plan.
  Each measured run must report all devices passed in its device-pass result. Filtered runs,
  failed or unmeasured runs, and older admission records without a verdict do not count.
  A full device lane without a budget or a qualifying timing uses its lane timeout. A run with
  arguments or `--expect` keeps the shortest bounded run. Queuekeeper reads the devices from the command's
  `--device` options. A lane whose script calls `device_pass.run` names them in
  `"devices": ["iPhone"]`. Admission preserves the lane's place in the queue.
- It runs the command once on each device as `devices run` does: bounded by the reservation,
  refused when it names another device, watched for a notification banner, and run once more
  after one ([bounded device runs](shared-host.md#bounded-device-runs)). `{udid}` and `{key}` in
  its words become the device's hardware UDID and its entry's key, which the command also finds
  in `QUEUEKEEPER_DEVICE_UDID` and `QUEUEKEEPER_DEVICE_KEY`, with a results directory of its own under the lane's.
  A project script that builds first, before any device is held, calls
  `device_pass.run(KEYS, COMMAND)` from Python instead, where COMMAND may be a function of the key
  and UDID that gives each device its own steps. The apps the command launches, `--launched
  BUNDLE` (`launched=`), join each entry's launched apps before its run, and `--apps-quit` says
  how the command quits them, which the device's release records.
- The pass exits 0 only when every device's command passed. A failure on any device fails it; a
  device not run, a run its bound stopped or a run a notification reached twice leaves it
  unmeasured, never passed, so `eng release` stops until it passes or the owner waives it. Each
  device's outcome is in the lane's results, `device-pass.json`, by key and with no device
  identity. A device it took and could not release stays held, said with its release command,
  and the pass exits 2.
- The devices run one after another, the width one: each device's run is bounded by its own
  reservation and watched for its own notifications. Name the iPhone first when its window is
  shorter than the iPad's wait.
- What the command does on the device is the project's, as are its own restoration steps, such as
  switching VoiceOver off after a session stopped part way ([VoiceOver](https://github.com/vocaro/AXVerify/blob/main/guides/voiceover.md#physical-iphone-and-ipad)).
  A check that needs a listener or the owner's hand stays a manual step of the release, recorded
  on its tracking issue.

## Declared devices

A device is a resource of the farm, not of a Mac: an iPad paired over Wi-Fi is reachable from every
Mac, and one kept to a cable from one Mac only. So the owner declares what should be true of each
device, Queuekeeper reads from each Mac what is, and what is read never adds a device to the farm or
takes one out (the owner's decision, 2026-11-17, [#211](https://github.com/vocaro/queuekeeper/issues/211);
[scheduling D7](scheduling.md#d7-no-mac-is-special)). Nothing is placed by a declaration yet: a device
lane runs where it did before, under its reservation.

### The declaration

Each physical device's declaration has two settings:

- **`reach`**: the ways a Mac may reach it, in the owner's order of preference: `cable`, `network`, or
  both. A way not listed is never one to use. `cableOn` names the Mac its cable is expected on, by its
  name in the farm or the name of the Mac that keeps the record, when `reach` lists a cable.
- **`absent`**: what it means when no Mac reaches it: `error`, for a device that should always be
  there, or `offline`, for one that may be away.

The owner's three cases are, in order:

```sh
qk devices declare iPad --by owner --reach network --absent error
qk devices declare iPad --by owner --reach cable,network --cable-on studio --absent error
qk devices declare iPhone --by owner --reach cable,network --absent offline
```

A device is declared under its entry's key, and only once the entry records its hardware UDID, which
its first reservation does. Declaring again replaces the declaration, and `--clear` removes it. A
declaration that names a way, a value of `absent` or a Mac it does not know is refused, and nothing is
changed.

The record keeps the declarations beside its entries, in `declarations`, by key, as it keeps the
owner's notification setup:

```json
"declarations": {
  "iPhone": {"reach": ["cable", "network"], "absent": "offline",
             "declaredBy": "owner", "declaredAt": "2026-11-18T09:00:00+00:00"}
}
```

So a declaration changes no entry: a held, orphaned or uncertain entry keeps every field of its
reservation, its revision included, and its holder's next change still names the revision it read.
Only the record that holds physical devices declares them; another Mac's record, which defers them,
refuses. An entry without a declaration behaves exactly as before.

### What each Mac reads

Each Mac lists its devices with `xcrun devicectl list devices --json-output`, read-only. Another
Mac is read over SSH in its login shell. On 2026-11-17 the listing took 0.17 to 0.42 s on the
owner's workstation and 0.56 s from the other Mac over SSH; each listed 67 or more simulators and
the two physical devices.

- **Reach is the transport.** `connectionProperties.transportType` is `wired` for a cable and
  `localNetwork` for the network. A simulator is `sameMachine` (or `reality` `simulated`) and is
  left out.
- **The tunnel is not reach.** `tunnelState` read `disconnected` for the iPhone while it was
  attached by cable and listed `wired`, beside the iPad's `connected`.
- **A device's way changes by itself.** On the morning of 2026-11-17 both devices read
  `localNetwork` on the owner's workstation; that evening they read `wired` there and
  `localNetwork` on the other Mac, at the same moment, with nobody declaring anything.

`qk devices status` lists, after the entries, each recorded physical device and each declaration,
with what each Mac read of it now: reached by a way, by a way its declaration does not list,
listed by no way a declaration may name, or not listed. A cable read on another Mac than the one
its declaration expects is said. A device a Mac lists that no entry records is listed as seen, not in
the record and not declared. A Mac that could not be read is named with why, and its devices are not
taken as unlisted. On the owner's two Macs, before anything was declared, with their names shortened:

```text
iPad: not declared. Read: workstation by cable; build-host by network
iPhone: not declared. Read: workstation by cable; build-host by network
```

On another Mac, whose record defers physical devices, status says that devices are declared in the
record that holds them, and lists what that Mac alone reads.

