# Recorded screen states

`ioreg -n Root -d1 -a` from both Macs on 2026-11-05, both unlocked, with the large
`IOKitDiagnostics` and `IORegistryEntryChildren` entries removed:

- `this-mac-unlocked.plist`: Trevors-MacBook-Pro-715, one login session, on the console.
- `second-mac-unlocked.plist`: Medias-MacBook-Pro-5, `trevorharmon` on the console and
  `mediaserver` not.

The others are those recordings with only the lock keys changed, as the locks measured on the
second Mac set them (guides/background-mac-testing.md, Lock screen and display sleep), since a
test never locks a real screen (vocaro-engineering#185):

- `*-locked-idle.plist`: a lock from idle time, as on 2026-10-28: the console session's
  `CGSSessionScreenIsLocked` true, `IOConsoleLocked` false.
- `second-mac-locked-by-hand.plist`: a lock by hand: both true.
- `second-mac-locked-without-ioconsolelocked.plist`: the hand-locked gate run of 2026-10-29:
  `CGSSessionScreenIsLocked` true, `IOConsoleLocked` absent.
- `second-mac-other-session-locked.plist`: only `mediaserver`, which is not on the console, locked.
- `second-mac-no-console-session.plist`: no session on the console.
