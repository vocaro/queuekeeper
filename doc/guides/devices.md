# Physical devices

How a session reserves a paired iPhone or iPad, or the members of a simulator pool, and what the plugins
do around a run on a device: silencing the owner's notifications, the device pass that runs a
project's device checks under one reservation, and the owner's declaration of each device with what each
Mac reads of it. How devices behave under test is in the toolkit's
[device testing](https://github.com/vocaro/apple-engineering/blob/main/guides/device-testing.md) guide.

The record, the device pass and declarations belong to the [mobile plugin](../../plugins/mobile/src/queuekeeper_mobile/README.md),
which the Apple and Android plugins share, so all of this applies to an Android phone too. The differences are in
the [Android plugin's notes](../../plugins/android/src/queuekeeper_android/README.md#physical-devices). An Android
device is named by its adb serial rather than a UDID, and its notifications are silenced with Do Not Disturb over
adb and read back, with no shortcuts to set up.

## Device reservations

Physical devices, and the members of a project's simulator pool, are used only under a real
reservation in the host's one device record. This section covers the record, taking a device,
bounded runs on it, and runs on another Mac.

### The device record

The record is a human-readable JSON file outside every repository,
`~/Library/Application Support/queuekeeper/device-handoff.json` of the account the sessions run as.

- `plugins/mobile/src/queuekeeper_mobile/devices.py`, which the Apple and Android extensions share, is its
  only writer. It finds the record from the
  account's home directory, not `HOME` or `TMPDIR`, so a session with a private environment joins the
  same record; `QUEUEKEEPER_DEVICE_RECORD` names an isolated record for tests only.
- Committed documentation keeps no device identifiers or live session details.
- Beside the device entries it holds `mac-foreground`, the owner's screen
  ([attended Mac sessions](foreground.md#attended-mac-sessions)), and the owner's declaration of each
  physical device, which `devices declare` changes without touching any entry. What each Mac reads of
  a declared device is kept in `device-reach.json` beside the record, never in it
  ([declared devices](#declared-devices)).

An entry records:

- the device and the owning session, with the revision the helper assigns;
- reserved, testing or released state, whether a run is active, and the process whose end ends the
  run; for a simulator pool member, the process that took it;
- the purpose, the expected release time and the bounded run being attempted;
- the original accessibility settings, and the apps, outputs and owner data to preserve;
- at handoff, the terminal outcome, the restoration checks and the explicit release time.

Read and change it through `qk devices`, which the mobile extension adds in a project that uses the
Apple or Android extension,
or from a script through `devices.Record()` and its `reserve`, `update`, `release` and `recover`
methods. Outside such a project, run it through Queuekeeper's own checkout, as the recovery commands
Queuekeeper prints do: `python3 ~/Development/queuekeeper/bin/qk --project ~/Development/queuekeeper
devices …`. `python3 -m queuekeeper_mobile.devices` finds its module only in a process `qk` started
(vocaro/queuekeeper#167).

```sh
qk devices status
qk devices show iPhone    # the entry and its revision; 0 if new
qk devices reserve iPhone --owner SESSION --revision N \
  --device UDID --purpose "…" --release-by 2026-10-21T18:00:00Z \
  --set 'originalSettings={"voiceoverEnabled": false}' --set 'preservation=…'
qk devices update iPhone --owner SESSION --revision N --set 'run=…'
qk devices release iPhone --owner SESSION --revision N \
  --outcome "…" --restoration "…"
```

### Changing an entry

The record is a coordination record, not a mutex: agree ownership with the other session first.

- Each change takes an exclusive lock, reads the record fresh, refuses unless the entry is still at
  the revision the caller read and, for an update or release, still the caller's, and replaces the
  file whole without rewriting any other entry. The lock covers one change, never a reservation.
- A refusal changes nothing: read the entry again and decide afresh. Each change prints the entry's
  new revision, which the caller's next change names.
- One device is never held under two keys. Give every command the device's canonical uppercase UDID,
  never a name or `booted`; the helper takes nothing else.
- `update KEY … --own PID` records a process the session launched, by its PID and start time;
  `release` and `recover` refuse until each has exited.

**Legacy records.** Sessions once kept ad hoc records in `/private/tmp`, where two could be live at
once, unlocked writes lost entries, and a restart emptied them while device work ran. The helper
refuses a record there. `devices import PATH… --by SESSION` merges such a file once, by its SHA-256,
leaving the file untouched. Every entry survives: a held version stays current over a released one,
the later activity wins between equals, the other version is kept in `importedVersions`, and two
holds of one device leave it uncertain. `status` names a legacy file whose current bytes were never
imported; once it is imported, stop writing it.

### Releasing and recovering

**A device is released only explicitly.** A passed expected release time, an idle screen or a silent
session proves nothing: obtain the release and verify the device's state freshly.

- A deadline never releases a device. `status` reports an overdue entry and changes nothing, and
  `doctor` warns of one whose silencing of the owner's notifications is not yet put back
  ([an overdue silencing](#an-overdue-silencing)).
- The one exception is a simulator pool's reservation whose processes are all gone and whose
  simulator is shut down, past its expected release or, for a run's reservation, once nothing
  accounts for its owner: reap releases it as a recovery
  ([stale pool simulators](disk-cleanup.md#stale-pool-simulators)).
- Release yours during offline analysis instead of holding an idle reservation.
- `release` refuses while a run is active: record its confirmed terminal result first.

**Orphaned and uncertain entries.** Preserve a live or uncertain reservation until recovery confirms
its work has stopped, as a heavy-work guard is preserved. Marking a run active records its process
(`hostProcessIdentifier`) and its start time. An active run whose process is gone, or has another
start time, is an orphan; an entry marked `uncertain` or `restoration-unverified` is uncertain.
Neither is reserved again until its owner releases it or an attended recovery is recorded, by the
owner or a session they agreed, once the run's processes have stopped and the device's settings and
apps are verified:

```sh
qk devices recover iPhone --by SESSION --revision N --verified "…"
```

Recovery refuses while the recorded process still runs and, for a run sent to another Mac, while
anything there may still use the device ([physical devices and another Mac](#physical-devices-and-another-mac)).

[Simulator testing](https://github.com/vocaro/apple-engineering/blob/main/guides/simulator-testing.md)
covers the pool's take, use and release steps. A physical device's reservation covers installing and
launching there, not operating the device by hand; [device testing](https://github.com/vocaro/apple-engineering/blob/main/guides/device-testing.md)
has the preflight, UI-automation authorization and restoration steps.

### Taking the iPad or iPhone in turn

The record schedules the paired iPad and iPhone, first come first served, one holder at a time, and
records no approval (owner's decision, 2026-11-06). Whether a session may use a device is between
the session and the owner:

- **The iPad** carries his standing approval (2026-10-31): take it whenever it is free, without
  asking.
- **The iPhone** is his personal phone (2026-11-01): ask him first, take it only once he has agreed,
  with `--release-by` at the end of the time he gave.

Both are taken the same way:

```sh
qk devices take iPad --purpose "…" --release-by 2026-10-31T09:00:00Z \
  --set 'originalSettings={"voiceoverEnabled": false}' --set 'preservation=…'
```

- `take` joins the entry's line, waits while another session holds the device or is ahead of it,
  saying who holds it and until when, and reserves it until `--release-by`. It prints the entry,
  whose revision the session's next change names.
- `--wait SECONDS`, an hour by default, bounds the wait, after which it leaves the line and reserves
  nothing. Start it in the background when the wait may outlast the tool's. Nothing hurries the
  holder, so a wait behind one that never releases ends at `--wait`.
- The line is kept in the record's `queues`, so joining it never changes the holder's revision. Each
  place records the waiting `take` process and its start; a place whose process is gone drops out.
  While anyone waits, `reserve` refuses everyone but the first in line.
- `take` refuses an entry that has never recorded its device (its first `reserve --device` does), and
  an orphaned or uncertain entry, which needs attended recovery.

**Notifications.** Reserving or taking a physical device also silences the owner's notifications on
it, with the shortcuts he made there once, and releasing or recovering it puts them back; the entry's
`notifications` records each step. Without those shortcuts the command says what to ask the owner.
`devices run` watches the device's log for a banner and runs a command one reached once more
([notifications during a run](#notifications-during-a-run)).

**Apps.** Record each app the session launches with `devices update KEY --launched BUNDLE`. Releasing
an entry that records any requires `--apps-quit "…"`, saying how they were quit (for example
`devicectl device process terminate`). The Shortcuts app that `devices take` opens for silencing
needs no such word. The next entry starts without them; its `previousLease` keeps them. An app running
on a device a session holds is a leftover the holder may quit, but never uninstall or clear beyond
the agreed cleanup.

### Device windows

A physical device's window is the reservation itself, until `--release-by`, and a bounded run stops
before that (owner's decision, 2026-11-06). The record refuses `--approved-*` on a physical device,
a hardware UDID of 8 and 16 hex digits or 40; only `mac-foreground` records the owner's approval. A
reservation made before then that recorded an approval stops once it has ended:
`devices.require_window(KEY, entry)` refuses, `qk remote --device` refuses, and `status` reports the
work as refused. An ended window releases nothing; the session restores the device and releases it.

### Bounded device runs

Run a command on a physical device through the toolkit's one launcher for a bounded run:

```sh
qk devices run iPhone --owner SESSION --revision N --limit 180 \
  --restoration 60 -- xcodebuild test-without-building … -destination "platform=iOS,id=UDID"
```

A project's launcher calls it from Python, `devices.BoundedRun([KEY], owner, command, revision=N,
limit=…, restoration=…).execute(command, stdout=log)`, and adds its own restoration queries and
release around it, as Pagepour's mobile UI session does. `qk remote --device` runs the same checks
and bound on another Mac ([below](#physical-devices-and-another-mac)). The launcher:

- **requires a held entry:** the session (`--owner`, else `QUEUEKEEPER_DEVICE_OWNER`, else the agent
  session) holds KEY with no run active, at `--revision` when given, and KEY names a physical device.
  It never acquires, extends or changes a reservation;
- **requires the window** the entry records and refuses once it has ended (`devices.require_window`);
- **refuses a command that names another device**, running nothing: a hardware UDID in any case, or
  any device the record holds under another key, a simulator among them;
- **runs the command supervised under a bound:** the reservation's remaining time, less 105 s for the
  run's own end (supervision's grace, SIGTERM, settling and SIGKILL waits) and `--restoration`
  seconds, 60 by default, for restoration queries, and at most `--limit`. It refuses a bound under
  30 s. The bound counts `xcodebuild` start-up, which can take minutes on a busy Mac, and counts
  waking time, so a Mac that sleeps through a run lengthens it by the sleep;
- **records the run:** marks it active with its own PID as the run's process, records the command's
  process as the entry's own, and records the outcome in `run` once the command and its observed
  descendants have ended: `exit N`, which is also its status, a timeout (status 124), a stop (the
  launcher then ends by the same signal) or a command that did not start;
- **treats an empty test run as unmeasured:** a command that exits 0 with a test count and no test
  run, every one skipped or none counted, returns status 3. `xcodebuild` has reported
  `** TEST EXECUTE SUCCEEDED **` after `Executed 0 tests` (vocaro/queuekeeper#100). `--no-tests`
  keeps 0 for a run meant to select none; a command that prints no count, such as an install, keeps
  its status;
- **leaves an unconfirmed end uncertain:** the run stays active and the entry uncertain, for attended
  recovery;
- **updates only its own entry,** naming the revision its previous change returned. A change the
  holding session makes during the run, such as extending its window, does not keep the run's end
  from being recorded: the launcher rereads the entry while the same owner holds it with this run
  active ([#230](https://github.com/vocaro/queuekeeper/issues/230));
- **never releases the device,** changes VoiceOver, uninstalls apps or deletes data. The session's
  next change names the revision the launcher's last change returned (`devices show KEY`).

**Which hex strings name a device.** A hardware UDID is 8 and 16 hex digits, or 40. A simulator's
UUID and a longer run of hex digits, such as a SHA-256, name no device. Nor do 40 digits that the
current directory's Git knows as an object (`git cat-file -e`), such as a commit's whole SHA-1, or
that name a code-signing identity's SHA-1 where a signing setting takes them: right after a name
ending in `IDENTITY=`, after `--identity`, or after `-s` or `--sign` in a `codesign` command. The
launcher cannot recognize a device's name or CoreDevice identifier, so name the device by its UDID.

### Restoring and releasing

A project's launcher keeps ownership and records `restoration-unverified` when restoration fails.
Read the launcher's terminal output and verify its receipt before reporting a release.

- An interrupted test bypasses its own restoration code. After a transport failure or a forced
  termination, query the settings independently before releasing; if connectivity prevents that,
  record restoration as unverified and coordinate recovery.
- An uninstall timeout has an unknown outcome: query the exact bundle's presence, retry removal only
  if it is still installed, and verify absence separately.
- To release, verify the original accessibility and keyboard settings, the absence of owned test
  processes and the measured build identities. Remove only the exact owned app and runner bundles,
  verify their absence, release the entry, and tell the other session that the device is released,
  including any unresolved restoration state.

### Physical devices and another Mac

**A physical device is reserved only in this Mac's record,** the Mac that sends runs to another Mac;
another Mac's record holds only its own simulators. The paired iPhone and iPad are reachable from
every Mac, so a record on each would let two runs use one device. Another Mac never reads this Mac's
record, which would give code there a way into this Mac; a run carries the reservation it may use.

**Deferral.** Every `qk remote` run first makes another Mac's record defer physical devices to the
sending Mac's (`devices defer MAC`, kept as `physicalDevicesRecordedOn`, which `status` reports).

- A record that defers them refuses to reserve a physical device or give an entry one, and names the
  record that holds them.
- A record there that already defers to another Mac, or holds a physical device, stops the run as a
  configuration problem for the owner: only `devices defer MAC --replace` changes the Mac (`--end`
  ends a deferral), and a held device is released or recovered first.
- The setup runs in the toolkit the sending Mac sent, which is newer when a shared checkout that is
  behind sends `origin/main`. Only the record's own refusal, status 2, is reported as the record's
  configuration; a toolkit that lacks the command's module is reported as a version difference,
  before any record is read; any other failure is the command's ([#155](https://github.com/vocaro/queuekeeper/issues/155)).
- `qk doctor` reports each Mac's records under `deviceRecord`. It is an error for this Mac's record
  to defer physical devices, or for another Mac's to hold one or defer them to another Mac; one that
  defers to no Mac yet is listed as unchecked until the next run.

**Running with a device.** A run on another Mac uses a physical device only under a reservation
held here, which it carries:

```sh
python3 scripts/qk remote admit --device iPhone -- COMMAND
python3 scripts/qk remote run --device iPhone LANE -- ARGUMENTS
```

- `--device KEY`, repeatable, makes the dispatch a [bounded run](#bounded-device-runs) of the
  reservations held here, with the same checks; otherwise nothing is sent. `--device-owner` names the
  session, else `QUEUEKEEPER_DEVICE_OWNER`, else the agent session.
- The dispatch marks each entry's run active with the `qk remote` process as its process, records the
  connection's `ssh` as the entry's own process, and records the outcome, host, work and results
  directory in `run`. Its end, by a stop, its death or a dropped connection, stops the run there, so
  a run whose dispatcher vanished is an orphan here.
- The bound starts when the dispatch does, so it counts sending the commits and waiting for admission
  there. Once it passes by this Mac's clock, the dispatch stops the run, and the outcome begins
  `stopped at its Ns bound`. The dispatch reports the time left and cleanup allowance before sending
  and again when the work starts, and warns, advisory only, if cleanup and an explicit
  `--time-limit` or lane `timeout` exceed the time left. Checkout preparation also consumes the
  window.
- The run reads its reservations from `QUEUEKEEPER_DEVICE_LEASE`:
  `{"record": MAC, "devices": {KEY: UDID}, "until": END}`. There `devices run KEY -- COMMAND` takes
  KEY from the lease, checks the devices the command names against it, bounds the command by END,
  and leaves the recording to the dispatch.
- The dispatch watches each leased device's log from this Mac and reruns a command a notification
  banner reached (doc/guides/devices.md, Notifications during a run); a `devices run` in the work
  watches from another Mac as well.
- The device stays reserved after the run. Preflight, restoration and release are the session's,
  from this Mac.
- Without `--device`, a command or lane arguments that name any hardware UDID are refused, whether or
  not this record holds the device: reserve it here and add `--device KEY`. `qk run` keeps a lane
  whose arguments name a physical device of this record on this Mac, and `qk admit` a command that
  names any hardware UDID.

**Recovery after a run there.** `devices recover` of an entry whose run went to another Mac asks
that host first. It refuses while the host lists any process whose command line names the device's
UDID, or any process left over in a checkout or the temporary directory since the run started
([processes left over from runs](disk-cleanup.md#processes-left-over-from-runs)). The run's
`offload_host.py`, `xcodebuild` and `devicectl` all name the UDID; a process that reaches the device
without naming it is not seen. An unreachable Mac keeps the device until it can be reached, or until
the owner recovers it with `--offload-unchecked REASON`, which the recovery records.

**Dependency commits.** A dependency commit can be absent from the caller's Git. Declare it with
`qk remote admit --git-commit /path/to/dependency=FULL_SHA -- COMMAND FULL_SHA`.

- The repeatable option verifies the full 40-digit hash as a commit in that local repository before
  sending anything; a missing object, a tree, a tag object, an invalid hash or an unreadable
  repository refuses the dispatch.
- Only an exact, separate positional argument is exempt; `id=FULL_SHA`, a hash after an option, or a
  hash inside a shell string still follows the device checks. A device the record knows always needs
  its reservation.
- The command receives the hash unchanged; the path is used only for local verification. Declare
  each foreign commit separately.
- The refusal of 40 hex digits that no entry holds names `--git-commit` and its limit; that of an
  8-16 UDID does not (vocaro/queuekeeper#91).

**Limits.** The records constrain only work that goes through the toolkit: `xcodebuild` or
`devicectl` run by hand on another Mac, or Xcode there, reaches the devices without them. A lane that
needs a device says so with `"here"`, so sending one to another Mac is always an explicit
`qk remote --device`.

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
- Each shortcut is opened only after a bounded check that this Mac's CoreDeviceService answers for
  the device ([a stuck CoreDeviceService](#a-stuck-coredeviceservice)), so a stuck service fails
  the silencing or restoration in about 30 s, said as a stuck service, rather than after the
  launch's own 120 s.
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

### An overdue silencing

A held physical device past its expected release whose entry still records `restore: pending`
may keep Do Not Disturb on the owner's device, and with Share Across Devices his others, after
the time he agreed. On 2026-11-15 the iPhone stayed so about 21 hours (vocaro/queuekeeper#174).
`qk doctor` warns of each such entry, and `devices status` prints it as an `Overdue silencing:`
line, naming the holder and the command that puts the notifications back:
`qk devices notifications restore KEY --owner SESSION --revision N`. The holder runs it, or
releases the device, which restores them too; otherwise tell the owner. Saying so changes
nothing: like any overdue entry, it restores and releases nothing by itself. Whether Queuekeeper
should restore the notifications itself at the expected release, or notify the owner, is open
for the owner's decision.

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
- Before any `devicectl` work on a device, the owner's shortcut included, it checks once that this
  Mac's CoreDeviceService answers for it. A device it does not answer for is not run, and its
  part is unmeasured, saying why and what fixes it
  ([a stuck CoreDeviceService](#a-stuck-coredeviceservice)); a device the pass took goes back.
- It runs the command once on each device as `devices run` does: bounded by the reservation,
  refused when it names another device, watched for a notification banner, and run once more
  after one ([bounded device runs](devices.md#bounded-device-runs)). `{udid}` and `{key}` in
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

## A stuck CoreDeviceService

CoreDeviceService on the Mac can get stuck while its devices stay reachable: `devicectl list
devices` still lists them, as `available (paired)` or `connecting`, but every command to one hangs
until its timeout (the toolkit's [device testing](https://github.com/vocaro/apple-engineering/blob/main/guides/device-testing.md#diagnosing-a-device)
guide, Diagnosing a device). On 2026-11-17 a release's device pass found out only after XCTest's
30-minute allowance and the pass's own bound, about 90 minutes, and putting back the owner's
notifications then waited out its 120 s
([#227](https://github.com/vocaro/queuekeeper/issues/227)).

So the device pass, before each device's run, and each notification shortcut, before it is opened,
first run `xcrun devicectl --timeout 30 device info details --device UDID`, read-only. On a healthy
service it answered in 0.19 to 0.23 s for the owner's iPhone and iPad on a cable, and refused a
device CoreDevice does not know in 0.14 s (2026-11-18, Xcode 27.2).

- An answer lets the work start.
- No answer within 30 s, or within the 15 s beyond that Queuekeeper waits for `devicectl` itself,
  for a device this Mac's `devicectl` lists is a stuck service. The pass's part for the device,
  or the shortcut's failure, says so and names the fix.
- No answer for a device `devicectl` does not list, or a refusal, such as a device not unlocked
  recently, is said as the device's, with `devicectl`'s words, and the work does not start either.

The fix is to restart the service: once no other device run is in progress on that Mac, quit
Device Hub and run `pkill -x CoreDeviceService`; launchd starts a new one at the next request.
The restart breaks every device session on the Mac, so Queuekeeper names it and does not run it.
Whether it should restart the service itself when no device run holds the Mac is left to the
owner (#227), as is whether a restoration that could not reach the device should be retried once
after a fresh connection.

## Declared devices

A device is a resource of the farm, not of a Mac: an iPad paired over Wi-Fi is reachable from every
Mac, and one kept to a cable from one Mac only. So the owner declares what should be true of each
device, Queuekeeper reads from each Mac what is, and what is read never adds a device to the farm or
takes one out (the owner's decision, 2026-11-17, [#211](https://github.com/vocaro/queuekeeper/issues/211);
[scheduling D7](scheduling.md#d7-no-mac-is-special)). Nothing is placed by a declaration yet: a device
lane runs where it did before, under its reservation, and what is read only reports.

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

### When no Mac reaches it

Each reading of a declared device, by `qk devices status`, `qk doctor` or `qk queue health` (and the
dashboard's Farm refresh, which reads the same), is remembered beside the record, in
`device-reach.json`: when a Mac last reached the device by a declared way, since when none has, and the
same for each way on each Mac. A Mac that could not be read changes nothing there. Nothing is written to
the record itself, and a record that declares no device is not read at all.

A declared device is then one of:

- **Reached**, by a declared way on some Mac.
- **Unreached, settling**: reached by no Mac, for less than the [settle time](#the-settle-time).
  `status` says for how long; nothing else does.
- **Unreachable**: reached by no Mac for the settle time or longer, measured from the first reading
  that missed it. What follows is the declaration's `absent`:
  - `error`: an error in `qk doctor` (its `errors`, and exit 1), in `qk queue health`'s Devices sign
    (danger) and in `qk devices status`, naming the device, the ways tried and each Mac's reading.
  - `offline`: `status` and the Devices sign say it is offline and since when. It is no error, no
    warning and no alert.
- **Not known**: reached by no Mac that was read, while another Mac was not read. `doctor` warns,
  and the Devices sign is unknown, for a device declared always reachable; the clock does not start.

Either way the device stays in the record, its reservation stands, and its entry is unchanged. A way
the declaration does not list counts for nothing: an iPad declared `network` and attached only by a
cable is reached by no Mac.

### The settle time

A device counts as unreachable only after it has been missing for **300 seconds**, by one way on one
Mac or by every declared way on every Mac (`reach.SETTLE`). One drop is not a decision.

On 2026-11-17, read every 15 seconds on both Macs for 20 minutes, no device lost or changed its way,
and no listing failed: the readings bound how long a listing takes, not how long a drop lasts. A
listing took 0.38 s at the median and 3 s at most on the owner's workstation, and 0.66 s and 1.6 s on
the other Mac over SSH, inside `devicectl`'s own 10 s wait. The way does change by itself over hours
(above). So the settle time is chosen to be:

- far longer than any one reading, and than a few missed ones, so a slow or dropped listing, a device's
  Wi-Fi joining again after sleep or a cable moved to another port is not an absence;
- shorter than the 600 seconds after which `qk queue health` calls a gate's wait slow, so a device a
  gate needs is named as unreachable before the wait is.

A device that comes back within the settle time is reached again, and its next miss starts the time
afresh. A device last read long ago is not taken as missing since then: its time starts at the first
reading that misses it, so a single `qk doctor` after a quiet night says it is settling, and the error
comes once a later reading, at least 300 seconds on, still misses it.

