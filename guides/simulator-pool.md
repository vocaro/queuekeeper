# Simulator pool

The Apple plugin's pool of reserved simulators: taking and releasing members, keeping or resetting their
state, warmed templates, pruning, and the settings a take writes. How simulators behave, and how to test
with them, is in the toolkit's [simulator testing](https://github.com/vocaro/vocaro-engineering/blob/main/guides/simulator-testing.md) guide.

## Reserved simulator pool

Reuse simulators. A project's pool is its simulators named `PROJECT DEVICE N` on their runtimes,
such as `Pagepour Duo 2`; engineering.json's `simulatorPool` names it (`Pagepour`). A member's
kind is its device type and runtime. Resetting a member, by copying its kind's
[warmed template](#warmed-templates)'s data into it or, failing that, erasing it, keeps its UDID,
and with it the simulator tool's approval, so a pool interrupts the owner far less than a new device
per run.

1. Take a shut-down member of the kind you need under a real reservation in the
   [device record](shared-host.md#device-reservations), keyed by its name. A booted member is
   another session's even when it looks idle, and so is one whose entry is held; if `simctl boot`
   reports it already booted, another session took it first, so choose another.
2. Observe it still shut down under the reservation, then reset only that member: Queuekeeper erases it
   and copies the kind's [warmed template](#warmed-templates)'s data into it, so that it and
   xcodebuild's clones of it answer their first request for the system's voices in seconds, and
   erases it alone when the pool has no settled template to copy, saying why, or when the lane asks
   for an erase (`--erase`); unless the lane [keeps its state](#keeping-a-members-state). A reset or
   erased member counts as fresh synthetic
   state for a qualification record: the template's data holds a finished first boot and
   VoiceOver's and the voices' first uses, and nothing installed.
3. Boot it to `bootstatus -b` under a time limit. A boot past its limit leaves the work
   unmeasured, never failed.
4. Pass its canonical uppercase UDID to `xcodebuild` as `id=` and to any simulator tool as its
   device.
5. Copy evidence off before shutting it down: the next session may erase it. Shut it down, confirm
   the shutdown with a fresh observation, then release the reservation. When the shutdown or the
   reservation cannot be confirmed, keep the reservation for recovery.

When no member of the kind is free, or the pool has none, create the next one under the pool's
naming scheme without asking the owner: reserve its name first, so two sessions never create the
same member, clone it from the kind's [warmed template](#warmed-templates) or else create it,
record its UDID, and let its first boot finish (`bootstatus -b`) before shutting it down. A shutdown right after a first boot kills daemons that are still starting and
leaves crash reports. The new member stays in the pool. A new member takes the lowest number no
simulator or held entry of that name uses, and reuses the label the pool already gives its kind
or device type, or else the device type's name (`EPUBLib iPhone 17 Pro 1`). CoreSimulator can store
a device after `simctl create` is interrupted and before it prints the UDID: recover only by the
member's exact name, and if more than one device has that name, stop and inspect instead.

Each Mac has its own pool and device record. A lane run on the second Mac takes or creates its
members among the second Mac's simulators and reserves them in the second Mac's record, so the
two Macs' members of one name are two simulators, and a reservation on one Mac holds nothing on
the other ([simulators on the second Mac](shared-host.md#simulators-on-the-second-mac)).

Never delete or rename a pool member automatically, except by pruning.

```sh
xcrun simctl erase UDID   # Queuekeeper then copies the warmed template's data in (Warmed templates, below)
xcrun simctl boot UDID
xcrun simctl bootstatus UDID -b
# Tests use -destination "platform=iOS Simulator,id=UDID"; copy evidence off, then:
xcrun simctl shutdown UDID
```

`python3 scripts/qk simulator-tests` carries out these steps for several devices at once: it
takes or creates one member per device type, builds the tests once while every member boots,
then tests every member side by side
([parallel tests](parallel-tests.md#simulators-side-by-side)). For a project's own runner,
`qk simulator-pool take --device TYPE --runtime RUNTIME [--keep-state | --erase]` reserves, resets
from the kind's template (or erases: with `--erase`, or when no settled template can be copied; neither
with `--keep-state`) or creates, and boots a member and prints its UDID, exiting 3 when the boot
reaches its limit. A taken member is held for an hour (`--hold MINUTES` for longer): the take/release
leases the record kept lasted 2 to 6 minutes and the longest pool lease 42, and once its expected
release has passed, reap may release a member whose session has gone (shared-host.md, Stale pool
simulators). A session that needs it longer runs `qk simulator-pool extend UDID [--hold MINUTES]`,
which moves its expected release to that many minutes from now, an hour by default;
`qk simulator-pool release UDID` shuts it down and releases it, and
`qk simulator-pool release --mine` releases every member the owner holds from a `take` whose
command has ended, leaving a running lane's own member to it and exiting 1 when it keeps any.
Closeout runs it, so a session that ends cleanly leaves no member to wait out its
expected release (vocaro-engineering#179). So that the hour is sized from measurement, each take, extend
and release of a member taken this way (by UDID or `--mine`), and each reap release of one, appends a line to
`~/Library/Application Support/vocaro-engineering/simulator-holds.jsonl`, beside reap's
`simulator-reaps.jsonl` (`HEAVY_SLOT_DIR` moves both): its `event` (`take`, `extend`, `release` or `reap`),
`pool`, `key`, `udid`, `owner`, `time`, the reservation's start (`reservedAt`) and `heldSeconds` since it,
`expectedReleaseBefore` and `expectedReleaseAfter` (none once it has ended), how it `ended` (the release's
outcome, or `Released by reap: …`; none while held) and, for a release, its `form` (`udid` or `mine`). Past
4 MiB the log becomes `simulator-holds.1.jsonl`, one generation kept. A log that cannot be written is warned
about on stderr and fails no command. `--outcome` names what the record says of a release; by default
`Released` by UDID and `Released at the session's closeout` with `--mine`. Each names the reservation's
owner: `--owner`, `QUEUEKEEPER_DEVICE_OWNER`, or the agent's session. Booting is heavy work: outside
an admitted lane, take the member under admission, which passes its printed UDID through
(`udid=$(python3 scripts/qk admit --here -- python3 scripts/qk simulator-pool take ...)`).
`qk simulator-pool check UDID` prints a member's canonical UDID and exits 1 for any other device,
a name or `booted`. A script that takes a UDID from its caller checks it first, so a device outside
the pool, such as one of Xcode's default devices, fails loudly instead of filling up with the
project's apps: from 2026-10-06 to 2026-11-01 scripts that picked a device by name put 56 GB on the
owner's Mac's defaults (vocaro-engineering#91). A script that drives a member its caller took runs
`qk simulator-pool check --held UDID`, which also exits 1 unless the owner (named as `take`
names it) holds a live reservation of the member, so a UDID copied from another session's member,
or from one nobody holds, fails before it drives a device another session is testing on; an
orphaned or uncertain reservation is refused until its recovery (vocaro-engineering#122). Xcode makes its defaults again when it installs a
runtime; `qk doctor` deletes each one that is shut down and unused, warns of any left, and
`qk disk --delete` deletes them too, without asking the owner. `qk simulator-pool list`
shows every member, whether pruning may delete it, and what its last take left it with: the
template it was reset or cloned from and when that was made, the state it kept, or why it was
erased; then every template. `qk simulator-pool reap [--dry-run] [--here]`
shuts down a member or warmed template left booted for 2 hours that no live run, process or reservation
accounts for, on both Macs, without asking the owner, and also runs before work is sent to the second Mac, before heavy work runs on the
Mac it was started on, and in `qk doctor`; to keep one
running, reserve it or boot a simulator outside the pool
([stale pool simulators](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#stale-pool-simulators)).

### Keeping a member's state

A lane that needs no fresh installation takes its member without the reset or erase:
`qk simulator-pool take --keep-state`, `qk simulator-tests --keep-state`, or
`pool.member(..., keep_state=True)`. The default take still resets, or erases; a lane that wants
pristine data rather than the template's asks for the erase with `--erase` (`erase=True`), and the
member's entry records that it was asked for (owner's decision, 2026-11-04). Everything else holds: a real
reservation, a fresh observation that the member is still shut down, its UDID, a boot to
`bootstatus -b` under a limit, and a shutdown and release however the run ends. A member another
session holds, or one that is booted, is never touched. The reservation's preservation note says the
member was not erased and what it carries, and its entry records `keptState`.

A kept member carries whatever its earlier holders left: their apps and app data, settings such as
the appearance, text size and VoiceOver's, and the finished first boot. A test whose expectation
depends on any of that takes the erasing default: a fresh-installation audit, a first launch,
default settings, or staged files, as in BreezEPUB's Ways In. A lane that keeps state still sets
what it relies on, such as the appearance or the volume, rather than trusting what the last holder
left.

What a kept member saves is its boot and VoiceOver's first start, not the erase,
which with the take took a few seconds either way (Xcode 27.2 (27B52), iOS 27.2 runtime 24B8117,
iPhone 17 Pro members, November 2026,
[vocaro-engineering#146](https://github.com/vocaro/vocaro-engineering/issues/146)):

- On the second Mac with the host to itself under exclusive admission, a kept or cloned member
  booted in 3.5 to 4.5 s and an erased one in 21.5 to 25.5 s, five sessions each, alternated; on
  this Mac at one-minute loads of 150 to 360, in 1.6 to 2.2 s against 21.4 to 22.5 s. On the second
  Mac near a load of 1,000, kept boots took 5, 31 and 188 s, the last ending with the build beside
  it, and erased ones 25 and 37 s.
- BreezEPUB's `tests` lane boots its member beside the build. With the second Mac to itself and the
  build warm (4 s), the boot is on the critical path: a kept run took 99 to 102 s and an erased one
  119 and 127 s, three runs each alternated, the erased boot 22 s against 4 s. Most of the rest is
  xcodebuild's test step, 93 to 100 s either way, of which XCTest's own testing took 8 to 18 s
  erased and 21 to 23 s kept. Under load, with a cold build, the lane's 122 to 318 s went to the
  build (69 to 190 s) and that step (28 to 165 s) in either mode; there QuickLook's first thumbnail
  after an erase took 5 s, and once failed after 53 s, where it took 2 s on a kept member.
- With the second Mac to itself, VoiceOver was silent 26 to 45 s after `enable()` on each erased
  member and spoke at once on each kept or cloned one; on this Mac under load the difference did not
  show ([readiness](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md#readiness-and-silent-start)). Most kept or cloned members
  put their volume back to 60 3 to 17 s after they were muted
  ([muted speech capture](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md#muted-speech-capture)).

### Warmed templates

Members are reset from, and new members cloned from, their kind's warmed template, `POOL LABEL
template (RUNTIME)` such as `EPUBLib iPhone 17 Pro template (iOS-27-2)`, instead of erased to
pristine data or created and booted for the first time (`queuekeeper.apple.simulators.template`).
The member pattern `POOL LABEL N` never matches a template, so no lane takes, tests on or prunes
one, and `simulator-pool check` refuses its UDID. A reap may shut one down, under the same
conditions as a member, by the owner's decision of 2026-11-04
([stale pool simulators](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#stale-pool-simulators)); pruning deletes one only when its
runtime is no longer installed.

- When a session takes or creates a member and its kind has no current template, it makes one under
  the template's own reservation, keyed by its name: it creates the template, boots it to
  `bootstatus -b`, reads the guest's runtime build ([runtime identity](https://github.com/vocaro/vocaro-engineering/blob/main/guides/simulator-testing.md#runtime-identity-in-evidence)),
  switches VoiceOver's [sound cues](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md#sound-cues) off and confirms them off (or
  leaves VoiceOver cold), mutes it until the volume has read 0 twenty times in a row (a reading with
  no answer neither counts nor breaks the run, within 90 s), switches VoiceOver on until the speech
  extension logs its first phrase (`Processing markup for voice locale`), switches it off and confirms
  it off. Then it uses the system's voices as a test process does, with a program `simctl spawn` runs
  in the simulator (`src/swift/Simulator/simulator-voices.swift`: `AVSpeechSynthesisVoice.speechVoices()`,
  then each language's own voice), shuts the template down, boots it again, uses them once more and
  shuts it down. A template whose second use answered is settled.
- **Why the voices.** The first request for the system's voices on a freshly erased simulator makes
  its accessibility assets daemon build its voice database in memory, an idle wait that grows with
  the host's load: 285 to 344 s in each of a member's xcodebuild clones with the second Mac to itself,
  1,359 to over 1,900 s beside another gate, which on 2026-11-04 left 8 of 12 EPUBLib gates there
  without a verdict. Clones of a member that had used its voices once waited 50 to 79 s; after two
  uses with a reboot between, 11 to 13 s. The member's own first use takes 53 to 173 s at rest, so
  the template pays it once for every reset and clone of its kind, not every gate on its critical
  path ([vocaro/EPUBLib#946](https://github.com/vocaro/EPUBLib/issues/946),
  [vocaro-engineering#172](https://github.com/vocaro/vocaro-engineering/issues/172)).
- **The reset.** A take of an existing member reserves the kind's settled template, erases the
  member, replaces the member's data folder with a clone of the template's (`clonefile(2)` on the
  whole folder, with its symbolic links as links: APFS makes it in a moment whatever the size, the
  files sharing their blocks with the template until written, every mode, owner, time, flag and
  extended attribute kept; a 2.2 GB template of 16,810 entries cloned in 0.24 s, where `cp -c -R`
  took 2.3 s and `cp -c -R -p` 12.5 s and added a provenance attribute to 9,209 files) and releases
  the template. The member keeps its `device.plist`, and with it its UDID, its name and the
  simulator tool's approval; `simctl clone` would make a device with a new UDID, which the device
  record keys on and the tool asks the owner to approve again. A copy, whether made by the reset or
  by `simctl clone`, keeps the template's absolute paths. Its symbolic links into the template's
  data and logs, and LaunchServices' store (`var/db/lsd/*.csstore`), resolve the member's
  app-group containers into the template's folder. Files' storage is one of them, so lanes staged
  books into the template, and members reset from it shared one storage
  ([vocaro-engineering#216](https://github.com/vocaro/vocaro-engineering/issues/216)). So after
  every reset and clone, the pool points each link that names another device at the member's own
  data and logs, and removes the copied store, which the next boot rebuilds. The pool refuses a
  copy that still names another device: a reset falls back to an erase and records why, and a
  clone is erased and recorded as created. `launchd_bootstrap.plist` is rewritten at every boot.
  The erase first means a copy that fails
  leaves an erased member, which the take then uses and says so (`TOOK … erased: …`); so does a take
  while another session holds the template past a 60 s wait, or when no template can be warmed, and
  one asked to erase (`--erase`). The member's entry records `resetFrom` (the template, its UDID, the
  build it was warmed on and when), or `erasedBecause`, and its preservation note says it is a copy
  of that template. A take with `--keep-state` copies nothing. Reset by default, erase as the
  fallback and on request, and a take that warms a missing template: the owner's decision of
  2026-11-04.
- A take warms within shorter limits, so that a kind's first take or creation inside a lane does not
  eat the lane's timeout: 300 s for the boot, the probe's 180 s start limit for VoiceOver and 300 s
  for each use of the voices, which with the 90 s mute hold bound it under twenty minutes; on a quiet
  host VoiceOver's part took about two (122.5 s on 2026-11-04). A template whose VoiceOver stayed
  silent that long is still cloned from, booted once but VoiceOver cold, and its record says so; one
  whose voices did not settle within the take's limits is still cloned from but never copied into a
  member, and such a take erases the member and names the command below.
  `qk simulator-pool template --device TYPE --runtime RUNTIME` warms with the full limits, 600 s for
  the boot and VoiceOver and 900 s for each use of the voices, and replaces a template whose
  VoiceOver stayed cold or whose voices did not settle; `--refresh` remakes it whatever its state.
  Run it ahead of a lane on a new runtime, or after a lane's take reports a cold or unsettled template.
  On 2026-11-04 the whole warm-up took about four minutes a kind on the second Mac at rest (iOS 27.0
  build 24A434): VoiceOver spoke 57 to 58 s after it was switched on, the voices listed 2.3 and 5.7 s
  into the first use, which followed VoiceOver's, and 32.3 and 28.0 s into the second, after the
  reboot; on this Mac on iOS 27.2 build 24B8117, 7.5 s, 0.3 s and 3.8 s. A reset then took 0.6 to
  0.7 s, and the reset member's own first use listed the voices in 2.6 s, where an erased member's
  took 53 to 173 s at rest. In two EPUBLib gates on the second Mac with the host to itself (toolkit
  `105e2ea`, 2026-11-04), each of the four xcodebuild clones a gate, two a device, listed the voices
  0.1 s into its first use, where the queue's last gate before the change had waited 534 to 606 s
  in each clone; the members reset in 0.7 to 0.8 s and booted in 15 to 18 s, and each device's run
  took 908 to 965 s with 2,528 tests executed and the 25 usual skips, against 34 before, nine of
  them the 500 s voice skip ([vocaro-engineering#172](https://github.com/vocaro/vocaro-engineering/issues/172)).
- The template's device-record entry records `template`: its UDID, device type and runtime, the
  guest build it was warmed on, the runtime's installed builds at the time, when, its boot time, what
  VoiceOver did, and `voices`: each use's seconds until the voices were listed and their count, the
  reboot's seconds, whether they settled, and why not. A member's entry records `clonedFrom` or
  `resetFrom`, and its preservation note says it is a clone or a copy of that template, with
  nothing installed.
- A template is current while simctl lists the same installed builds of its runtime as when it was
  warmed and its record holds its voices. When the builds change, as when a runtime build is
  installed or removed ([OS drift](https://github.com/vocaro/vocaro-engineering/blob/main/guides/os-drift.md)), or the template was warmed before templates
  settled the voices, the next take or creation of the kind deletes the template under its
  reservation and warms a new one; so it does with one that never finished warming, and with one
  whose VoiceOver devicectl could not confirm off. VoiceOver's setting is in the data container, so a
  clone or copy of such a template would boot with VoiceOver on and speak through the Mac's output
  once its volume came back ([muted speech capture](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md#muted-speech-capture));
  a template warmed so is recorded, shut down and never cloned or copied. devicectl's reading is of the
  running system, so the pool also reads what the shut-down template saved, `VoiceOverTouchEnabled` in
  `Library/Preferences/com.apple.Accessibility.plist`, after each warm-up and before each clone or copy,
  and treats a template saved with it on the same way. On the second Mac on 2026-11-09 one of the 25
  shut-down templates on both Macs was saved so, though devicectl had confirmed it off: its members booted with
  VoiceOver on, and XCUITest found system Settings' toggle rows empty, with no switches, while the
  screen drew them. Copies of a template saved off, and erased members, were complete
  ([vocaro-engineering#263](https://github.com/vocaro/vocaro-engineering/issues/263)).
- A template another session holds past the wait, or one that is booted, is never touched: the
  member is erased, or created with `simctl create` and first-booted as before. So is a member whose
  clone failed and stored nothing, and every member while simctl cannot list the runtime's builds.
- A warm-up killed outright leaves its template booted and held by a process that is gone. Reap shuts
  it down once that reservation's expected release has passed and the template has run 2 hours, and
  then releases its entry ([stale pool simulators](https://github.com/vocaro/vocaro-engineering/blob/main/guides/shared-host.md#stale-pool-simulators)). Once that entry is released, the template has
  no record of a finished warm-up, so the next take or creation of the kind replaces it.
- A clone has a UDID of its own, as a created member has, so the simulator tool's first use of it
  asks the owner, as for any new member ([simulator lifecycle](https://github.com/vocaro/vocaro-engineering/blob/main/guides/simulator-testing.md#simulator-lifecycle)); a reset
  member keeps its UDID and approval. The template itself is never handed to a tool.
- `qk simulator-pool list` shows each member's last take, the template it was reset or cloned from
  and when that was made, or why it was erased, and each template, current or stale, and why.
  [`qk disk`](shared-host.md#reclaiming-disk-space) keeps a template while its runtime is installed
  and lists one whose runtime is gone, which `qk simulator-pool prune` deletes under a reservation
  and records.

`simctl clone` copies its source's data container. Each of twelve clones of a warmed EPUBLib iPhone
17 Pro member, six on each Mac, held the source's files, 7,033 to 8,541 of them and 1.9 to 2.7 GB,
with the same names and sizes, and the probe app the source had installed; each clone took 3.7 to
6.2 s. A clone booted as fast as a kept member and ran its source's runtime build, 24B8117 of
iOS-27-2's two installed builds. With the second Mac to itself, VoiceOver spoke at once on each of
five clones, its first phrase 10 to 11 s after the test started against 44 to 64 s on an erased
member, and at once on a clone of a member warmed by `devicectl` alone, the template's recipe, which
had taken 63 s to speak on that just-erased member
([vocaro-engineering#146](https://github.com/vocaro/vocaro-engineering/issues/146)). So a clone
saves VoiceOver's first start as well as the first boot
([readiness](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover-simulator.md#readiness-and-silent-start)).

### Pruning

A session may delete a pool member an hour after its last use and release, when all of these hold:
it is not the lowest-numbered member of its kind (device type and runtime), so every kind keeps one
member and its approval; `simctl` shows it shut down and its `lastUsedAt` is more than an hour old;
and its device-record entry is released, or it has none, and any release is also more than an hour
old. Reserve it first, check again that it is still shut down and unchanged, delete it by UDID, and
record the deletion in the device record. Delete the highest-numbered eligible member first. A new
member takes the lowest free number. A member whose runtime is no longer installed
follows the same rule.

The hour is a grace for a lane between two takes, not an idle limit: no longer one keeps a member
(owner's decision, 2026-11-07,
[vocaro-engineering#259](https://github.com/vocaro/vocaro-engineering/issues/259), replacing the 14
days of 2026-10-26). Every take resets its member from the kind's warmed template, so a kept member
is no warmer than a new clone of it. On the first Mac that day, under one admission at load 38 to 47
on 18 cores, the reset took 3.0 and 8.1 s against takes of 100 to 250 s whose boot costs the same
for a reused or a new member, and a clone takes 3.7 to 6.2 s
([vocaro-engineering#146](https://github.com/vocaro/vocaro-engineering/issues/146)). The lowest-numbered
member, always kept, is the one a `--keep-state` take finds first and the one the simulator tool has
approved. The 14 days had kept 29 members, 74 GiB, there. `simulators/pool.py`'s `PRUNE_AFTER` holds
the hour.

A session checks for eligible members when it takes or creates one of the same kind:
`qk simulator-tests` and `qk simulator-pool take` prune that kind first, and
`qk simulator-pool prune [UDID ...]` prunes on request. The reservation is made at the entry's
revision that was found eligible, so a use in between refuses it, and the deletion is recorded
as the entry's `pruned` field and its release. [`qk disk`](shared-host.md#reclaiming-disk-space)
lists eligible members of every pool a main checkout names, with the prune command.

### App accessibility

UIKit and SwiftUI answer for their own views (a `UILabel`'s label, a `UISwitch`'s value, every
SwiftUI element) only in an app process the system has loaded their accessibility into, which it
does while an assistive technology runs or while the simulator's `ApplicationAccessibilityEnabled`
setting in `com.apple.Accessibility` is on. A test that reads them in process, such as an
[accessibility snapshot](https://github.com/vocaro/vocaro-engineering/blob/main/guides/voiceover.md#accessibility-snapshots) of system views, asks for the
setting: `qk simulator-tests --app-accessibility`, `qk simulator-pool take --app-accessibility`
or `pool.member(..., app_accessibility=True)` (owner's decision, 2026-11-06,
[vocaro-engineering#181](https://github.com/vocaro/vocaro-engineering/issues/181)). It does
nothing for a hostless test bundle, whose `xctest` process is no app: the test bundle must be
hosted in one. VoiceOver stays off; switching the simulator's VoiceOver on
(`xcrun devicectl device settings voiceover --enable`) is the fallback should the setting stop
working.

- **The key is undocumented.** Apple documents neither it nor any other switch for this in
  `simctl` or `devicectl`. It was verified on the iOS 27.2 simulator, runtime build 24B8117
  (Xcode 27.2, 27B52), iPhone 17 Pro and iPad Air 13-inch (M4) members, 2026-11-06: written after
  the boot with `xcrun simctl spawn UDID defaults write com.apple.Accessibility
  ApplicationAccessibilityEnabled -bool true`, it loaded UIKit's and SwiftUI's accessibility into
  a hosted test bundle's app launched afterwards, with VoiceOver off, and did nothing in a
  hostless bundle. The [OS-drift](https://github.com/vocaro/vocaro-engineering/blob/main/guides/os-drift.md#undocumented-simulator-settings) procedure checks
  it again on each new runtime build or Xcode.
- **Set on every take, not in the template.** Queuekeeper writes it after each boot of a member whose
  lane asks, and reads it back as `1`; a setting that does not read so fails that device before
  its tests start (`FAIL KIND: its app accessibility could not be switched on`) and the take
  command exits 1, since the lane's hosted tests would read no system views. A
  [warmed template](#warmed-templates) could not hold it for every take: a take that resets copies
  the template's data, but one that falls back to an erase, or asks for one, starts from pristine
  data without it; a kept member carries whatever its last holder left; a template warmed before
  this change would hold nothing until its runtime moved; and one template serves every lane of
  its kind, most of which do not want every app process to load the frameworks' accessibility.
  A lane that does not ask has the setting
  deleted on a member that kept its state, so that a kept member's apps read system views only
  when their lane asks; a reset or erased member cannot carry it, and nothing is spawned for it.
- **The test process is told.** With `--app-accessibility`, `qk simulator-tests` gives each test
  process `ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS=1` (as `TEST_RUNNER_ACCESSIBILITY_SNAPSHOT_SYSTEM_VIEWS`,
  unless the caller set it), and the golden API then fails every golden read in a process that
  has no system views, naming the setting, instead of comparing an empty tree.
- Only pool members are touched, through their reserved, booted UDIDs; the owner's own
  simulators and Xcode's default devices never are. xcodebuild's parallel workers (`--workers`)
  are clones of the booted member: they were not checked to carry the setting.

## Stale explicit modules

Derived data reused across a dependency change can keep an explicit module built from the old
header: the build fails with `has been modified since the module file`, and then with errors for
whatever the new header added. `qk simulator-tests` recognizes that message in a failed build,
removes that derived data's `Build/Intermediates.noindex/SwiftExplicitPrecompiledModules`, and builds
once more. Its output says `STALE MODULES` with what it removed and how the rebuild ended, and
`build-stale-modules.log` keeps the first build's log. A rebuild that fails, even on the same message,
is that run's failure; it does not build a third time. Its tests use a stand-in xcodebuild: the
message came from two real runs on 2026-10-31, where removing that folder by hand cleared it, and the
automatic path has not yet met a real occurrence.

A lane that calls `xcodebuild` or `swift` itself gets the same recovery from `qk run` (#267). qk run
puts `bin/shims` first on every lane's PATH, here and on the second Mac, so each of those tools a lane's
script calls by name runs through a wrapper, wherever the script sends its output. A wrapped run that
fails with that message removes the module cache the message's module file is in
(`SwiftExplicitPrecompiledModules`, `ModuleCache.noindex` or SwiftPM's `ModuleCache`), only inside the
checkout the lane runs in, and runs the same command once more; it never runs a third time. The
output, in whatever log the script keeps, says `queuekeeper: STALE MODULES` with what was removed and how the
rerun ended, and the lane's `results/stale-modules.log` records it. A script that calls
`/usr/bin/xcodebuild` by path or through `xcrun`, or clears PATH, bypasses the wrapper. Under qk run,
`qk simulator-tests` reads that notice and does not rebuild a third time. Its tests use stand-ins;
no real occurrence has yet met it.
