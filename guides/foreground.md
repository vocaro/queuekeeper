# Foreground runs

How Queuekeeper takes a Mac's foreground for a lane that needs its screen, such as Mac VoiceOver or Mac
XCUITest: setting the second Mac up for it, running and waiting, and the guards that clear crash alerts and
Notification Center windows. Its limits, what counts as evidence and how to batch sessions are in the
toolkit's [second Mac foreground](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md) guide.

## Setting up the second Mac's foreground

A command Queuekeeper runs on the second Mac starts over SSH. It runs in an audit session of its own,
yet the window server gives it the console user's session
([which Mac runs a lane](shared-host.md#which-mac-runs-a-lane)). Foreground work there needs
settings of the owner's Mac that the hosted workflow once made on each disposable runner. They
are the owner's to change, once; Queuekeeper and its runs change none of them. `qk remote foreground`
lists each one missing with its fix, and which run holds the foreground. Every foreground run
checks them first and, while one is missing, stops before it takes anything: a
[configuration problem](https://github.com/vocaro/vocaro-engineering/blob/main/agents/core.md#shared-resources) for the owner, exit 78 from the
command.

```sh
python3 scripts/qk remote foreground    # 0 ready; 78 with what the owner changes, there
```

1. **The owner's session on the screen.** The account Queuekeeper runs as is logged in on the second
   Mac's own screen, and no other account is switched in.
2. **An unlocked screen that stays so.** In System Settings, Lock Screen: the screen saver and
   turning the display off never, and no password after either. A locked screen puts the login
   window in front, which a key or VoiceOver's cursor then reaches instead of the app. On
   2026-10-30 the second Mac had the login window in front at one look and Safari at another.
   Waking the display is a run's own: an admitted test run may hold `caffeinate -u` and then
   `caffeinate -d` ([background Mac testing](https://github.com/vocaro/apple-engineering/blob/main/guides/background-mac-testing.md)), but nothing unlocks
   a screen.
3. **Accessibility for commands started over SSH.** Remote Login runs them under
   `/usr/libexec/sshd-keygen-wrapper`: add it in Privacy & Security, Device Control and Data
   Access, and turn it on. macOS 27.2 gives Accessibility that name (its `ACCESSIBILITY` string in
   `SecurityPrivacyExtension.appex`); the list's link anchor is still `Privacy_Accessibility`. Without it `AXIsProcessTrusted()` is false for such a command (macOS 27.2 26B123,
   2026-10-30), so the [accessibility observer](https://github.com/vocaro/VoiceCheck/blob/main/guides/voiceover-mac.md#accessibility-observer) exits 2
   and System Events sends no key.

   **System Events needs Accessibility of its own.** In the same list, turn on System Events
   (`com.apple.systemevents`; add `/System/Library/CoreServices/System Events.app` if it is not
   listed). System Events posts the keys a run sends through it, and posting them needs its own
   grant; the wrapper's does not cover it. Without it, the first key raises "“System Events”
   would like to control this Mac and access your data." on the second Mac's screen, from
   `universalAccessAuthWarn`. That is the Accessibility prompt, not an Automation one. On
   2026-11-12 (macOS 27.2 26B123) System Events asked for `kTCCServicePostEvent` at the first key
   over SSH, tccd denied it and raised the prompt, and foreground runs stopped on that alert,
   unmeasured, until the owner allowed it (vocaro/queuekeeper#13). The Mac's log for the ten days
   before holds no such prompt, so why it was needed only then is not known. To check it over
   SSH, read the system TCC database, which the wrapper's Full Disk Access lets such a command
   open; `2` is allowed, and no row or `0` means it is missing:

   ```sh
   sqlite3 -readonly "/Library/Application Support/com.apple.TCC/TCC.db" \
     "select auth_value from access where service='kTCCServiceAccessibility' and client='com.apple.systemevents'"
   ```

   `qk remote foreground` does not check this grant yet (vocaro/queuekeeper#18).
   Whether a command over SSH also needs Automation permission to control System Events is
   unmeasured: no Automation prompt has been seen there, the per-user TCC database does not open
   over SSH, and asking `AEDeterminePermissionToAutomateTarget` without a prompt needs System
   Events running.
4. **Screen recording for commands started over SSH.** Add `sshd-keygen-wrapper` in Privacy &
   Security, Screen & System Audio Recording too, in its Screen Recording list, not System Audio
   Recording Only (anchor `Privacy_ScreenCapture`). A run's screenshots use `screencapture`, and
   `CGPreflightScreenCaptureAccess()` was false for such a command on the second Mac
   (2026-10-30). Whether a capture without it fails, captures only the desktop or raises a prompt
   that takes the focus mid-session is unmeasured; a preflight is not a captured bitmap either
   ([capturing a background window](https://github.com/vocaro/apple-engineering/blob/main/guides/background-mac-testing.md#capturing-a-background-window)).
   With `sshd-keygen-wrapper` allowed and the preflight true, a session there on 2026-10-30 still
   met a system dialog in the middle: "“com.apple.sshd-session” is requesting to bypass the system
   private window picker and directly access your screen and audio", which took the focus and
   stopped it. That is the direct-capture approval described next, which the Screen Recording list
   and the preflight do not cover. The second Mac's record holds it under
   `/usr/libexec/sshd-keygen-wrapper`, last prompted 2026-10-30 18:36 UTC, the time of that
   session, and lapsing 2026-11-29, so a command over SSH that captures after that date meets the
   prompt again. Whether adding `/usr/libexec/sshd-session` to the Screen Recording list changes
   anything was not tried.

   **The direct-capture approval follows the app's path.** That dialog, "… is requesting to
   bypass the system private window picker and directly access your screen and audio", is
   separate from the Screen Recording list and the preflight: macOS records each Allow in
   `~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist`, keyed by
   the app's file URL (or a bundle ID for some apps), and asks again for any other path. On the
   second Mac (macOS 27.2 26B123, 2026-11-06) BreezEPUB's XCTest runner had eleven entries, one
   for each checkout slot and derived-data folder it had been launched from. One path kept its
   approval across 452 captures and many clean, re-signed rebuilds, so the signature and CDHash
   do not matter, and `CGPreflightScreenCaptureAccess()` returning true does not keep the prompt
   away. Launch a capturing runner or tool from one path per host, outside the checkout slots:
   `capture_approvals.stable_runner_path(PROJECT, BUNDLE)`, under
   `~/Library/Application Support/VocaroEngineering/foreground-runners`. Under the foreground
   lease, `capture_approvals.stage` copies the signed bundle there with `ditto`, refusing while one
   still runs from it, and `capture_approvals.rebind` points the xctestrun's paths at the copy
   ([vocaro-engineering#209](https://github.com/vocaro/vocaro-engineering/issues/209); BreezEPUB's
   Mac acceptance driver uses them). A check that binds a runner's signature to its path names
   both: the build output's path for the build records, and the copy's for the launched runner,
   which must match the latest build in everything but its path. The owner allows the stable path
   once. Reading the plist over SSH is harmless. Never edit it: that is not a supported mechanism.

   **Each approval lapses after 30 days.** Each entry also has `kScreenCapturePrivacyHintDate`,
   30 days after the last prompt (`kScreenCapturePrivacyHintPolicy` 2592000 s, 90 days for some
   apps), and macOS asks again after that date even for a stable path. The only supported way to
   stop the renewal prompt is the `forceBypassScreenCaptureAlert` restriction in a
   `com.apple.applicationaccess` profile (macOS 15.1 and later). The profile has to come from
   device-enrolled MDM; it cannot be installed by hand or through User Enrollment
   ([Apple's schema](https://github.com/apple/device-management/blob/release/mdm/profiles/com.apple.applicationaccess.yaml)).
   The second Mac has no MDM, and gets none: the owner allows each stable path again when its
   30 days lapse, about once a month, rather than run MDM (owner's decision, 2026-11-06). A run
   that meets the prompt stops unmeasured, as before; do not set up MDM or ask him about it again.
   `qk remote foreground` lists when each stable runner path's approval and
   `sshd-keygen-wrapper`'s lapse, or that they lapsed, read from that plist; a lapse does not
   change its verdict, since only a capture meets the prompt and only the owner answers it.
5. **XCTest's automation mode without authentication.**
   `sudo automationmodetool enable-automationmode-without-authentication`; the second Mac had it
   on 2026-10-30.
6. **No VoiceOver welcome dialog.**
   `defaults write com.apple.VoiceOverTraining doNotShowSplashScreen -bool true`. A Mac that has
   never run VoiceOver opens the dialog first (hosted runner, `xcode-27` image, macOS 27.0.1,
   October 2026). The second Mac had never run VoiceOver on 2026-10-30.
7. **No Dictation offer.** `defaults write com.apple.HIToolbox AppleDictationAutoEnable -int 0`.
   Every VoiceOver command presses Control, and after about four of them "Do you want to enable
   Dictation?" took the VoiceOver cursor (hosted runner, same image).
   On 2026-11-05, with `qk remote foreground` reporting the second Mac ready, a Dictation prompt
   asking for the owner's password appeared there during a run of menu sessions, keys sent every
   5 s. A session never answers it: the owner cancels it, which keeps Dictation off. Its cause was
   not confirmed; the sessions' own transcripts did not name it.

The hosted runner was prepared in other ways too, which the second Mac is not:

- XCTest's "Automation Running" banner is a system launch agent with `KeepAlive` while
  `/var/db/com.apple.dt.automationmode/automation-enabled` exists, and with
  `EnablePressuredExit`. On the hosted image it was killed and restarted about every ten seconds
  during a UI test, and VoiceOver moved to an instance that survived, which lost three sessions
  of seven; `launchctl bootout` unloaded it there only because the image ran with System
  Integrity Protection disabled. The second Mac has it enabled (2026-10-30), where bootout fails
  ("Operation not permitted while System Integrity Protection is engaged") and changes nothing.
  The banner stays: stop a session as soon as VoiceOver names it
  ([speech capture](https://github.com/vocaro/VoiceCheck/blob/main/guides/voiceover-mac.md#speech-capture-and-the-key-driver)). It restarts on the
  second Mac as it did on the image: in EPUBLib's runs there on 2026-10-30 (macOS 27.2 26B123) the
  kernel killed it for its per-process memory limit, 15 MB (`memorystatus: killing_specific_process
  ... per-process-limit`, with over 3 million pages free), about every 10 s, 20 starts in one
  run of two sessions, so a quiet host would not keep it. VoiceOver moved to a new instance in two
  sessions of seven, both 600 points wide, of three there; none of the four at 1100 points met
  it. With System Integrity Protection enabled no setting known here keeps the banner. The owner
  keeps SIP enabled there and has a session the banner stopped run again (owner's decision,
  2026-10-30): a project's session runner gives such a stop a status of its own and runs the
  session again, a bounded number of times within the lane's budget, keeping every attempt's
  transcript. A session the banner stopped is unmeasured, never passed or failed.
- The hosted runner's 1024 × 768 screen was set to 1920 × 1080 with `displayplacer`, because it
  held a window to 1024 × 674 and an accessibility resize to 1100 points reported success
  without resizing. The second Mac's screen is its own built-in display (3456 × 2234 Retina,
  2026-10-30), whose resolution is the owner's setting. Record the screen's bounds with every run
  and take every boundary from the session's own observer state.
- Builds were signed ad hoc, `CODE_SIGN_IDENTITY=- DEVELOPMENT_TEAM=`, needing no team,
  certificate or secret, and the image's own privacy grants were enough for the UI test. On the
  second Mac an ad hoc runner is unmeasured. Signing with a team needs its keychain in a GUI
  session, which an SSH command does not reach
  ([keychains on the right Mac](shared-host.md#keychains-on-the-right-mac)).
- Read actual on/off state with the canonical public AppKit getter under the foreground lease
  ([state and restoration](https://github.com/vocaro/VoiceCheck/blob/main/guides/voiceover-mac.md#voiceover-state-and-restoration)). Read and, if a run asks, choose VoiceOver's link verbosity
  with `voiceover-link-verbosity.swift` ([link verbosity](https://github.com/vocaro/VoiceCheck/blob/main/guides/voiceover-mac.md#link-verbosity)),
  never with `defaults`; a run that chooses one chooses the setting it read again before it ends.

## Running foreground work

- A project keeps its foreground work in a lane marked `"foreground": true`, with a heavy or
  exclusive resource. `qk run LANE [-- ARGUMENTS]` sends it to the second Mac, uncommitted work
  as a snapshot commit, as any lane goes ([how a run reaches the second
  Mac](shared-host.md#how-a-run-reaches-the-second-mac)), and `qk remote run LANE` does the
  same. It never falls back to this Mac: a second Mac that is not configured, cannot be reached
  or is not ready stops the lane, with what the owner can change.
- A stop is not the end of the work. When the second Mac cannot be reached, ask the owner for a
  window on [this Mac's foreground](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md#this-macs-foreground), saying what would run and for how
  long, rather than ending the turn on the stop; Queuekeeper's message names the command for the window.
  A misconfigured or unready second Mac stays the owner's to fix unless the owner offers one.
- `qk remote admit --foreground -- COMMAND` runs one command under the second Mac's foreground
  and its heavy-work admission, for work no lane holds; `--exclusive` with it holds the whole
  host for a timing measurement. On the second Mac itself, `qk admit --foreground -- COMMAND`
  holds them the same way. `qk admit` never sends foreground work to the other Mac: it runs where its foreground is held.
- The foreground is the `mac-foreground` entry of the second Mac's own
  [device record](shared-host.md#device-reservations), whose deferral of physical devices to
  this Mac's record marks it as the second Mac's. Queuekeeper reserves it for the run, recording no
  approval (vocaro-engineering#207), naming the reservation `queuekeeper PID on MAC`, records its own process as the entry's active run,
  and sets `QUEUEKEEPER_DEVICE_OWNER` for the work, so a driver's `devices check` before each key
  passes as on this Mac ([attended Mac sessions](shared-host.md#attended-mac-sessions)). The
  check confirms that the active run still runs by its start time; a driver inside an app's
  sandbox, such as an XCTest runner, may not start `ps`, and the check then reads that start from
  the kernel instead. A sandboxed runner's check once failed on `ps` alone, so its test skipped
  itself (2026-10-30, [vocaro-engineering#68](https://github.com/vocaro/vocaro-engineering/issues/68)).
  The release records the run's outcome and whether the application in front before the run is in
  front after it. Before it, a foreground run quits every app it opened and reports any it leaves
  running ([apps a run opened](#apps-a-run-opened)).
- Put arguments a session needs after `--`: the lane's command receives them, such as session
  settings as `KEY=VALUE` words that a project's session runner reads. Check a step list here
  first with the project's own check, such as `scripts/run-voiceover-sessions.py --check`; the
  lane checks it again on the second Mac, but only after the wait for the foreground.
- A lane's command writes what must come back, transcripts among it, in its results' `results/`,
  whose absolute path `qk run` gives it in `QUEUEKEEPER_RESULTS_DIR`: `.build/queuekeeper/LANE/results/`
  on the Mac that runs it. However the run ends, those files come back from the second Mac into
  `.build/queuekeeper/remote/LANE/results/` here, beside the run's log and receipt. Every run of a
  lane, on either Mac, begins with that `results/` empty, so a later run that writes none keeps
  none an earlier run left. Inside a lane group, each lane's `QUEUEKEEPER_RESULTS_DIR` is
  `.build/queuekeeper/GROUP/results/LANE/`, which comes back with the group's results, beside its
  lanes' logs and receipts. A command that names a fixed path instead, such as
  `.build/queuekeeper/LANE/results`, leaves a group's files on the second Mac: read the variable.
- `qk remote admit` sets no `QUEUEKEEPER_RESULTS_DIR` and brings no files back, and what its command
  writes inside the checkout is gone by the next look (2026-11-05). Write what you need outside
  the checkout, such as `~/Development/build-host/NAME/`, and copy it with
  `scp 'build-host:Development/build-host/NAME/*' DEST`, or make the work a lane.

## Waiting for the foreground

- One run holds the second Mac's foreground at a time. A run that finds it held says once who
  holds it, for what and until when expected, looks again every 10 s, and takes it as soon as it
  is released, for up to the 5,400 s that `qk run` waits for admission; then it ends with
  nothing run. A canceled waiter checks its stop signal before each reservation attempt, at
  most one 10-second poll later, and ends without starting its command or changing the holder.
  Its dispatch releases the checkout. `qk remote foreground` names the holder.
- A holder whose expected release falls after the waiter's deadline is judged at once
  (vocaro/queuekeeper#95). On 2026-11-12 a waiter spent its whole 5,400 s on a holder expected
  80 minutes past its deadline. Now its first line names the release, the deadline and the command
  to run again after the release, such as `qk run mac-menu-voiceover`. It keeps looking for
  `foreground.GRACE`, 180 s, since a holder can release early, and takes the foreground if it
  does. Then it ends with nothing run, exit 75 as when the wait runs out, naming the release and
  the command again. The grace covers a holder already ending: its command has up to 120 s between
  SIGTERM and SIGKILL, and its VoiceOver restoration settles after that. A release that comes later
  is no likelier in the next few minutes than in any others of the holder's window. A holder with
  no expected release, or one within the deadline, keeps the whole wait. A new holder, or a new
  expected release, is judged anew. Nothing waits past the deadline for a late holder.
- A run takes the foreground first and heavy-work admission after it, so a run waiting for the
  screen holds no slot. Holding the foreground excludes other foreground work only: builds and
  background tests keep running beside a session. A session that misbehaves only on a busy
  second Mac is a defect to fix in its timing, not a reason for exclusive admission.
- The entry outlives a run only as an orphan: a run whose Queuekeeper process died leaves its active run
  recorded, and every later run stops on it, naming it and the recovery command, until attended
  recovery on the second Mac confirms its work stopped
  ([recovering the foreground](#recovering-the-foreground)). A deadline releases nothing.
- VoiceOver on is a valid initial state, not evidence that a previous run failed. The owner
  authorized automatic capture, temporary off for quiet tests, and exact restoration under the
  lease (2026-10-30). `foreground.slot` uses
  `queuekeeper.apple.voiceover.state.preserved(owner, record=record)` after acquisition and
  active-owner marking. This default preserves existing clients' initial on/off behavior.
  A passive test opts into `state.preserved(owner, target=False, record=record)` inside that
  lease; it turns off only a known on state and restores on only when that was the original.
  No query or toggle runs while another session holds the lease. Readiness reports do not query
  VoiceOver or ask the owner to turn it off.
- Every query and Command-F5 rechecks the exact lease. Initial unreadable state refuses without
  changing anything. Every bound stretches with the one-minute load average per core, at most
  `state.STRETCH_LIMIT`, 24, times: on 2026-11-03, at loads near 217 on the second Mac, a fixed
  5 s Command-F5 timed out and VoiceOver came on after it (#137). A load under one per core while
  the CPU's busy share, as admission reads it, is 95% or more is doubted, and stretches by the
  limit, as a load that cannot be read does: on 2026-11-12 the second Mac's `getloadavg` fell
  from 951 to 307 in 27 s and rose from 7.45 to 250 in 12 s, each time with the CPU 99 to 100%
  busy, and a Command-F5 whose bound had stretched to only 5 s ended `KEY_LATE` while the load
  around it was 490 to 630 (vocaro/queuekeeper#43). A failed transition's error ends with the
  reading its bounds used, such as `(stretched 24 times, by a one-minute load of 7.45 on 10 cores
  with the CPU 100% busy)`.
- A transition that fails or times out inside a `preserved` scope fails the run, not the lease:
  the scope sets the original state again, under the unchanged lease, and watches it hold for a
  stretched `state.SETTLE`, 5 s, so that a key System Events delivers late is seen and undone, up
  to `state.RESTORE_ATTEMPTS`, 3, times. Verified, the lease releases and the failure carries
  `restored`; Queuekeeper releases the foreground. A restoration that is not verified, unknown final
  state and lost ownership retain uncertainty, and do not release the entry for the next session.
  A failed transition with no `preserved` scope around it is uncertain at once.
- Queuekeeper's own guard, around the run's child driver, always settles its restoration the same way
  (`preserved(settle=True)`): a child killed between a failed toggle and its own restoration
  tells Queuekeeper nothing, and its late key may still arrive. The foreground releases only once that
  settled check verifies the original state; otherwise it stays uncertain.
- A foreground run has `processes.FOREGROUND_KILL_AFTER`, 120 s, between SIGTERM and SIGKILL.
  A driver runs its session inside `voiceover.guard.stop_signals`, which raises `Stopped` where
  the session is, so it restores VoiceOver before the slower end of its probe and observer.
  The outer state guard preserves that handler or Queuekeeper's child-supervision handler and restores
  only after the child has ended. A lane's session runner ends its last session well before
  the lane's `timeout`, so only a stop reaches one mid-session. An unverified child's end keeps
  the foreground uncertain; the guard never toggles over that uncertainty.
- When one session leaves VoiceOver changed, the run's next session may set it back and carry on,
  rather than the run giving up (owner's decision, 2026-11-12,
  [EPUBLib#416](https://github.com/vocaro/EPUBLib/issues/416)). Queuekeeper records the state it captured
  on acquiring the foreground in the entry, as `voiceOverOriginal`, with the lease it was read
  under. Over a clean lease the session runner sets it back with `state.set_enabled(original)`.
  Over a lease that a session's own unverified restoration left uncertain, it calls
  `queuekeeper.apple.voiceover.state.recover_restoration(owner)`, which proceeds only when the
  owner and lease identity are unchanged, the run's host process is alive, the original was
  recorded under that lease, and the uncertainty is that restoration alone, with nothing written
  to the entry after it. It restores the original under that lease, rechecking it before every
  query and key, and watches it hold for a stretched `state.SETTLE`. Verified, the entry is no
  longer uncertain and its `voiceOverRestoration` records who recovered it and when; the next
  session starts. Otherwise it raises `TransitionFailed`, records the failed attempt, which ends
  the permission, and the entry stays uncertain. Any other uncertainty, an orphan, or a run that
  has ended still refuses with nothing changed and needs
  [attended recovery](#recovering-the-foreground).
- A session VoiceOver stopped at the banner runs again: the driver checks what VoiceOver said
  before each step with `guard.check_banner` and exits `guard.BANNER_STOP`, 123, when it names the
  banner, and the session runner runs each session through `guard.run_again`, at most
  `guard.BANNER_ATTEMPTS` times while the run's time allows, keeping every stopped attempt's
  transcript ([setting up](#setting-up-the-second-macs-foreground)). A runner that keeps one
  folder per session passes `before_again`, which sets the stopped attempt aside before the next
  one starts and returns where it was kept.
- Build before the screen when the build is long: a lane holds the foreground for its whole
  command, the build included. A project with a slow build can build in a lane of its own first
  and run its sessions from those products.

## Recovering the foreground

An uncertain or orphaned `mac-foreground` entry needs attended recovery, which is the owner's
decision, except a session's unverified VoiceOver restoration, which the live run's own runner
may recover once ([waiting for the foreground](#waiting-for-the-foreground)). On the second Mac, once the run's work has stopped:

1. Put VoiceOver back in the state the run's `host.txt` recorded at its start, with Command-F5.
   VoiceOver quit through AppleScript leaves `com.apple.universalaccess voiceOverOnOffKey` at 1,
   and `NSWorkspace.isVoiceOverEnabled` follows that key. The guard now reads an on that no
   VoiceOver process confirms as off, but set the key to 0 as well after such a quit:
   `defaults write com.apple.universalaccess voiceOverOnOffKey -bool false`, and check that
   `defaults read com.apple.universalaccess voiceOverOnOffKey` prints 0 (owner, #137).
2. Record the recovery, naming the entry's current revision, which `python3 -m
   queuekeeper.apple.devices show mac-foreground` prints and the refusal names:
   `python3 -m queuekeeper.apple.devices recover mac-foreground --by SESSION --revision N
   --verified "WHAT WAS CHECKED"`. It is not a `qk` subcommand.

## Apps a run opened

A foreground run quits every app it opened before it releases the screen. On 2026-11-12 an EPUBLib
`voiceover-mac` run that opened Safari for a MathML comparison released the second Mac's foreground
with Safari's window still in front; 6 of the next 7 foreground runs met it, and a Mac VoiceOver
session read it into its transcript ([EPUBLib#1034](https://github.com/vocaro/EPUBLib/issues/1034)).
A driver still quits what it opened as soon as it is done with it, as EPUBLib's runner now quits its
Safari after the last session; Queuekeeper's quit is the net under it
(`opened_apps.py`, vocaro/queuekeeper#104).

- **What it compares.** Once the run holds the foreground, Queuekeeper reads the regular apps
  running, those with a Dock icon, through AppKit's `NSWorkspace.runningApplications`. After the
  run's command and its guards have ended, and only while the `mac-foreground` lease is still the
  run's, it reads them again. An app running at the first look is never touched, whoever opened it.
- **What is the run's.** The second Mac is no workstation, and the run holds its whole foreground,
  so a new app is the run's when it is installed in an Applications folder (`/Applications`,
  `/System/Applications`, `~/Applications`, their `Utilities`, or Safari's cryptex), when it was
  built in the run's own checkout, or when the session recorded its bundle with
  `python3 -m queuekeeper.apple.devices update mac-foreground --launched BUNDLE`.
- **How it quits one.** It asks the app to quit by its process, as the Dock's Quit does, waits up
  to `opened_apps.QUIT_WAIT`, 30 s, for all of them, and never forces one: an app asking to save
  is left for the report.
- **What it leaves, and says so.** Any other new app is left running and reported, with why: one
  built somewhere else, such as a gate's test host in its own checkout or in DerivedData, may be
  another run's, since background lanes run beside a foreground one; one nested in another app,
  such as Xcode's Device Hub, may be a simulator lane's; one whose bundle already ran before the
  run is the owner's to have; and macOS's own, in `/System/Library/CoreServices`, is not the run's.
  An app list that cannot be read, at either look, quits nothing and says so.
- **Where it is reported.** The run's log has a line for each app quit or left, and the released
  entry's `appsQuit`, which its `restoration` repeats, says the same. A run that no longer holds
  the lease at its end quits nothing: an app opened after another run took the foreground is that
  run's.
- **Windows are not apps.** A window the run opened in an app that was already running, such as
  the Finder "Recents" windows a Quick Look corpus run left in 2026-11, stays; closing those is
  vocaro/queuekeeper#99.

The test suite never quits an app: `tests/host_isolation.py` sets `QUEUEKEEPER_QUIT_OPENED_APPS`
to `off` for every run a test makes, in its process and its children, so that each reads no
running app, and its tests of quitting use stand-ins for the app list and the quit.

## Crash-report alerts

An app that quits unexpectedly raises an alert, "APP quit unexpectedly.", in a `UserNotificationCenter`
window that stays on the screen until someone answers it. A foreground run's clicks under it reach the alert
instead: in EPUBLib's Mac toolbar runs on the second Mac on 2026-11-02 (macOS 27.2 26B123, Xcode 27.2 27B52),
the clicks on Search hit a MobileCal and then a ColorPickerUIService alert, both a `UserNotificationCenter`
window at x 734, y 201, 260 × 250, and the runs passed, 10 of 10, only once their windows were moved clear of
it ([EPUBLib#916](https://github.com/vocaro/EPUBLib/issues/916)). The owner wants these alerts on between runs,
so Queuekeeper handles them for every foreground run, a `"foreground": true` lane or `qk admit --foreground`, on
either Mac (`crash_alerts.py`, [vocaro-engineering#118](https://github.com/vocaro/vocaro-engineering/issues/118)):

- **Off for the run alone.** Once the run holds the foreground and its admission, Queuekeeper reads
  `com.apple.CrashReporter DialogType`, keeps that value in
  `~/Library/Application Support/vocaro-engineering/crash-dialog.json`, sets it to `none`, and logs both. However
  the run ends, passed, failed, stopped or raising, it puts that exact value back, deleting the key when it was
  unset, macOS's default, reads it back, logs it, and only then removes the record; the receipt's `crashAlerts`
  says what it was and whether it was put back. A run whose Queuekeeper died leaves the record, and the next Queuekeeper command
  on that Mac, whatever it is, puts the value back, unless the run that kept it still runs. Queuekeeper changes the
  setting nowhere else. A value the owner set to `none` already is left alone. The setting hides the alert
  only: macOS still writes the crash report (`.ips`), which a run collects as before
  ([outcomes](https://github.com/vocaro/vocaro-engineering/blob/main/guides/second-mac-foreground.md#outcomes-and-results)).
- **Cleared, never answered.** Before the run starts, and again 2 s after each look ends while it runs, Queuekeeper reads
  the windows of the processes that raise system alerts through System Events, which needs the accessibility
  grant the second Mac already has for commands started over SSH. The 2 s (`WATCH`) is the wait between looks,
  not their cadence: a look took under a second on 2026-11-03, but under load on the second Mac on 2026-11-07
  one System Events look took about 17 s and another about 30 s, and every click in that time can go to the
  alert ([vocaro-engineering#225](https://github.com/vocaro/vocaro-engineering/issues/225)). So each look during
  the run first glances at the window list (`CGWindowListCopyWindowInfo`, which gives a window's owner, layer
  and bounds without any permission and answers at once, as for
  [Notification Center windows](#notification-center-windows)), and asks System Events only when the list shows
  an on-screen window of one of those processes, a crash report being `UserNotificationCenter`'s at layer 8;
  when the list cannot be read; or when System Events was last asked more than 30 s (`RECHECK`) before, in case
  a process's name in the list differs from its name in System Events. A look that cannot read the alerts, a
  missing grant or a System Events call that failed or timed out, is followed by the next as any other: Queuekeeper
  says once that looks fail and once when one reads them again, and the receipt's `crashAlerts.looks` counts the
  looks that read the alerts, failed, and were spared by the window list. A run that fails with no look able to
  read them says so, and its failure stands. It dismisses a crash report, a window of `UserNotificationCenter`,
  `Problem Reporter` or `CrashReporter` that says the app quit unexpectedly, with Ignore, or OK, and logs each
  with its window's frame. It never presses Reopen or Report…, and never touches a password, permission or
  keychain prompt (`SecurityAgent`, `coreautha`, or any alert asking for a password, access or permission):
  those are the owner's.
- **Unmeasured, never failed.** A system alert Queuekeeper may not clear before the run stops it with nothing run, as
  unmeasured (exit 3), naming the alert and its frame. One met during a run that then fails, or still there as
  it ends, makes that run unmeasured too; a run that passes passed.
- **A cleared alert, by whose crash and how long it stood.** A crash-report alert Queuekeeper cleared during a run that
  then fails makes the run unmeasured when, and only when, it stood longer than one look and was for a process
  other than the app under test: its clicks may have gone to the alert, as EPUBLib's did under alerts that stood
  17 s and 30 s on the second Mac on 2026-11-07 (owner's decision, 2026-11-07,
  [vocaro-engineering#226](https://github.com/vocaro/vocaro-engineering/issues/226)). The process is the one the
  alert's text says quit ("Crasher944 quit unexpectedly."). The app under test is what a lane's `"apps"` names,
  or `qk admit --app NAME` (and `qk remote admit --app NAME`), each as the alert names its process. An alert
  stands from the start of the first look that listed it to the return of its press; one look is `WATCH`, 2 s,
  the wait between looks. A crash of the app under test stays a failure even if Queuekeeper also cleared another
  process's alert that stood longer than one look. Eng also preserves the failure when it cannot read the
  process name from the alert or the run names no app under test, and says why. A passing run stays passed.
  The receipt's `crashAlerts.cleared` records each alert's process, start (UTC), duration
  (`stoodSeconds`) and whether the run had started (`duringRun`). The verdict message names the alert.
- **Between steps.** A driver that wants to look between its own steps runs
  `python3 -m queuekeeper.mac_desktop.crash_alerts check` under the foreground it holds: it clears crash reports and
  exits 0, or 3 naming an alert it left, or 1 when the alerts cannot be read.
- **Reported.** `qk remote foreground` lists every system alert over the second Mac's screen, and says when
  crash-report dialogs are off with no run holding the foreground: left by a run that died, which the next Queuekeeper
  command there puts back, or set by someone else, which the owner puts back with
  `defaults delete com.apple.CrashReporter DialogType`.

On macOS 27.2 (26B123) no setting tried keeps the alert away, so clearing is what keeps a run going. On the
second Mac on 2026-11-03, a small AppKit app that calls `fatalError` a second after it starts, named apart for
each setting, raised its alert about a second after its crash with `DialogType` set to `none`, set to `server`,
with `UseUNC` set to 1, and with neither set, and macOS wrote its `.ips` each time. In each the run's watcher
dismissed it with Ignore within a look or two, and a run that began with such an alert left on the screen
dismissed it and passed. Queuekeeper still turns `DialogType` off for the run, as the owner asked, in case a later macOS
honors it. Under a load of about 200 on the second Mac, System Events answered slowly enough that a probe of
four crashes took about half an hour; a look gives up after 60 s and does not hold the run. Until 2026-11-07 a
first look that could not read the alerts left the whole run unwatched: in one of EPUBLib's crash-alert runs for
[EPUBLib#944](https://github.com/vocaro/EPUBLib/issues/944) that look failed once, though the runs before and after
it in the same hour read the alerts, and the alert then stood until the run ended (#225).

## Notification Center windows

A notification that Notification Center presents, a banner or an alert, shows in a window of
`NotificationCenter`'s own, at Quartz window layer 23, over every app's windows. On this Mac, on macOS 27.2,
that window covered the whole screen, 1728 × 1117, and was off the screen while no notification showed
(2026-11-06). On the second Mac on 2026-11-05 one stayed on the screen across two exclusive foreground
admissions, every look of a 60 s retry among them with Finder in front, so a BreezEPUB benchmark's preflight
never launched it until the owner closed it by hand
([BreezEPUB#15](https://github.com/vocaro/BreezEPUB/issues/15#issuecomment-6680023845)). Crash-report alerts are
`UserNotificationCenter`'s and the section above's; this is the other process
(`notification_center.py`, [vocaro-engineering#204](https://github.com/vocaro/vocaro-engineering/issues/204)).

- **Found by the window list.** Queuekeeper reads the on-screen windows with `CGWindowListCopyWindowInfo`, which needs
  no permission for a window's number, owner, layer and bounds, and never reads a window's name. A window counts
  when it is at layer 23, visible and of some extent, and its owner process runs
  `/System/Library/CoreServices/NotificationCenter.app/Contents/MacOS/NotificationCenter`: the list's owner name,
  "Notification Center" on 27.2, does not establish it alone. Every other window is left alone.
- **Closed only under the lease.** Once a run on the second Mac, a `"foreground": true` lane or
  `qk admit --foreground`, holds the foreground, its admission and the crash-report guard, Queuekeeper closes each
  notification such a window presents and looks at the window list again, every 0.5 s for up to 5 s, until the
  window has left the screen, up to three rounds for notifications that show after the last. Only then does the
  run start. Queuekeeper closes nothing on this Mac, whose notifications are the owner's, nor on the second Mac without
  the `mac-foreground` lease that `QUEUEKEEPER_DEVICE_OWNER` names: there it refuses.
- **How it closes one.** Through System Events, it performs the notification's own Close action, the one its
  close button performs, on an element whose subrole is `AXNotificationCenterBanner` or
  `AXNotificationCenterAlert`, found again by its subrole and frame. It never presses a notification, which
  opens it, never uses Clear All on a stack, and never touches one that asks for a password, permission or
  access, which stays the owner's. When the window shows anything besides banners and alerts, such as a stack
  or the open panel's history and widgets, Queuekeeper closes nothing in it, so no history entry goes. Closing one
  notification leaves Notification Center's other notifications,
  its history and its settings as they were.
- **Why accessibility, not something without it.** No route without the accessibility grant closes one
  identified notification. Escape goes to the app in front, and quitting `NotificationCenter` acts on every
  notification it shows rather than an identified one, and is not a close at all. The second Mac already
  grants accessibility to commands started over SSH, which `foreground.slot` checks before it takes the
  foreground ([setting up](#setting-up-the-second-macs-foreground)); Queuekeeper reads the grant again
  (`AXIsProcessTrusted`) before it closes anything, and without it the run is unmeasured, saying so.
- **Unmeasured, never failed.** A window list it cannot read, a window whose notifications it cannot read or
  close, one it may not close (a stack, a permission request, nothing it knows), or a window still on the screen
  after the last round stops the run with nothing run, as unmeasured (exit 3), naming the window by number,
  process, layer and frame. Queuekeeper prints and records only that, and each closed notification's kind and frame,
  in the receipt's `notificationCenter`; never what a notification says.
- **By hand.** `qk host notifications` lists such windows over this Mac's screen, exiting 0 when none is and 3
  when one is. A driver that wants to look between its own steps, under the foreground it holds on the second
  Mac, runs `qk host notifications --dismiss`, or `python3 -m queuekeeper.mac_desktop.notification_center check`:
  exit 0 once none is left, 3 naming a window left, 1 without the lease.

Checked live on the second Mac (macOS 27.2) on 2026-11-07, under admitted foreground runs:

- **The shape.** A banner showed in `NotificationCenter`'s window at layer 23 covering the whole screen, as an
  `AXGroup` of subrole `AXNotificationCenterBanner` under an `AXScrollArea`, with the actions `AXPress`, `Name:Show
  Details` and `Name:Close`. Performing Close took it off the screen, and the window with it. `entire contents` of
  the window lists no banner, so the scripts walk its UI elements. They name their variables with a trailing `_`,
  because inside `tell application "System Events"` `kind` is a property of its own, and `set kind to ""` failed
  (-10006). Before these two fixes Queuekeeper read no live notification at all, and each run stopped as unmeasured.
- **A run that proceeds.** One run left a banner on the screen. The next `qk remote admit --foreground` closed it
  before its command started, and the command found the screen clear. A test that closed a banner left a Finder
  window, the negative control, where it was.
- **A test notification that shows.** Use the owner's **Vocaro Test Notification** shortcut
  ([Notifications during a run](devices.md#notifications-during-a-run)), run there with
  `shortcuts run "Vocaro Test Notification"`. Shortcuts is authorized for notifications there. `display
  notification` from `osascript` is not: Script Editor has no `auth` in
  `~/Library/Group Containers/group.com.apple.usernoted/Library/Preferences/group.com.apple.usernoted.plist`.
  Its notifications are delivered to the store but never shown, with Do Not Disturb on or off, so such a
  notification is no live test there. A banner leaves the screen after about 5 s, sooner than System Events
  can read it on a loaded Mac. A test holds it with the pointer, a mouse-moved event over it (`CGEventPost`),
  and moves the pointer away when it ends, even on failure: a banner held past the test blocks the next run.
- **Do Not Disturb.** The owner's Focus is shared across his devices, so a change on the second Mac is a change
  on his iPhone. The **Vocaro Silence Notifications** shortcut run there turns Do Not Disturb on, then fails
  at Save File, since the second Mac has no iCloud Drive. Read `~/Library/DoNotDisturb/DB/Assertions.json`
  before changing Focus: on 2026-11-07 a test that assumed Do Not Disturb was still on turned it on again
  for all his devices, and **Vocaro Restore Notifications** run on this Mac turned it back off.

## VoiceOver state and restoration

- Use `queuekeeper.apple.voiceover.state`: `read(owner)` reads the actual running state through
  the public AppKit `NSWorkspace.isVoiceOverEnabled` getter. The bounded JXA query accepts only
  an exact boolean; missing, malformed or unreadable state refuses work. The getter follows
  `com.apple.universalaccess voiceOverOnOffKey`, which VoiceOver quit through AppleScript leaves
  at 1 (vocaro-engineering#137), so an on also needs this user's system VoiceOver process: an on that no such
  process confirms within a stretched 5 s reads off. An off is the getter's alone. Do not infer
  on from a process query, or anything from a missing preference.
- `preserved(owner, target=None)` captures only after the exact foreground lease is checked and
  restores that original state before the lease ends, on normal exit, error or a handled stop.
  Queuekeeper wraps second-Mac foreground work in this context with the default, which leaves the
  original on/off state in place. A quiet test explicitly uses `target=False`; a speech driver
  must enter its own local `preserved` scope, use `set_enabled(True, owner)` inside, and close
  its popup before restoration. Public setters share only that process/context's restoration
  provenance; Queuekeeper's parent-process guard cannot share it with the child driver.
  Each state read and toggle rechecks the lease. Never query or toggle while waiting for another
  holder. No state is changed when the strict initial read fails.
- The setter sends Command-F5 through System Events only when the known state differs and waits
  up to 5 s for the public getter to verify it. Every bound here stretches with the one-minute
  load average per core, at most 24 times (`state.stretched`): a fixed 5 s Command-F5 timed out
  at loads near 217 (vocaro-engineering#137). A load under one per core beside a CPU 95% busy or
  more is doubted and stretches 24 times (vocaro/queuekeeper#43); `state.stretch_reading()` gives
  the reading a driver records as its evidence. Before sending an ON key, a separate readiness
  check waits up to 20 s for this user's recognized system VoiceOver process to finish quitting,
  then rereads the public state. This uses the speech runner's existing quitting bound: public
  off and ready to enable are different observations. Process residence never makes an on by itself:
  it only confirms the getter's on. Unknown readiness or an expired bound sends no enable key. XCTest's
  `enable()` timed out on the Mac.

  A failed or timed-out transition inside a `preserved` scope raises `TransitionFailed`, and the
  scope restores the original state under the same exact lease, then watches it hold for a
  stretched 5 s so that a late key is seen and undone, up to three times. Verified, the error's
  `restored` is true and the lease releases. Unverified restoration, unreadable final state and
  lost ownership retain uncertainty instead of releasing the lease, as does a failed transition
  with no `preserved` scope to restore it. Queuekeeper's guard around a child driver passes
  `settle=True`, so it watches its own restoration hold even when the child died without a report.

How a driver ends a session, and what it writes when VoiceOver will not return to its state, is in the
toolkit's [Mac VoiceOver](https://github.com/vocaro/VoiceCheck/blob/main/guides/voiceover-mac.md#voiceover-state-and-restoration) guide.
