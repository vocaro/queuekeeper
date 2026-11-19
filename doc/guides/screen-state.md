# Screen state

This guide covers how a Mac's screen, locked or unlocked, display asleep or awake, affects where a lane
runs and how its result is read. The [user guide](../user_guide.md#screen-lock) gives the summary; read
this when a UI lane waits for a screen, ends unmeasured behind a lock, or you declare a lane that needs
the screen. Placement in general is in [Which Mac runs a lane](another-mac.md#which-mac-runs-a-lane).

## Reading a Mac's screen

No guide, policy or log line assumes any Mac's screen state. Queuekeeper reads it on the Mac that runs a
lane, with the check `qk host screen [--remote]` runs
([checking a Mac's screen](https://github.com/vocaro/apple-engineering/blob/main/guides/background-mac-testing.md#checking-a-macs-screen)),
and writes what it read, or `unknown` and why, never a guess (vocaro-engineering#185). The owner keeps
another Mac unlocked, but any Mac can lock, by hand or by idle time.

A command started over SSH runs in an audit session of its own and is not trusted for accessibility, yet
the window server gives it the console user's session: it opens offscreen windows and reads its own
accessibility. What a lock or a sleeping display stops, measured per Mac and macOS build, is dated
history in
[lock screen and display sleep](https://github.com/vocaro/apple-engineering/blob/main/guides/background-mac-testing.md#lock-screen-and-display-sleep).
In short, on macOS 27.2 whole gates passed behind a lock, but the accessibility API (`AXUIElement`)
vended no offscreen window there. A run whose mechanism a lock or a sleeping display stops is
unmeasured, never passed
([shared resources](https://github.com/vocaro/vocaro-engineering/blob/main/doc/agents/core.md#shared-resources)).

## Lanes that need an unlocked screen

A lane whose tests need an unlocked console screen, such as one that reads its own tree through the
accessibility API, says `"unlockedScreen": true`, and so does each group that runs it. A lane that does
not declare it is placed whatever any screen reads: a lock blocks only the work that needs the screen
unlocked (owner's decision, 2026-11-17, [#205](https://github.com/vocaro/queuekeeper/issues/205)).

- **Placement.** Each Mac's screen is read with its capacity, and the lane goes only to a Mac whose
  screen reads unlocked; `locked` and `unknown` both count as no room. The placement line says so:
  `the lane needs an unlocked screen, and another Mac's is locked (screen: locked (...) on NAME at
  HH:MM:SS)`.
- **Waiting.** Another Mac declines the lane before it takes a checkout while its screen is not unlocked,
  and this Mac does not admit it while its own is not. With no Mac unlocked the lane waits and is offered
  again like any waiting lane, rather than running unmeasured and using up the merge queue's no-verdict
  runs. A gate waits for the screen before it starts, holding no slot, and reads it again once its
  slots are granted: if the screen locked while it waited, it gives them back unstarted and waits again
  with its place's age, saying `queuekeeper: gate gave back its admission, unstarted, and waits: ...`.
  Without that second reading, a gate held a Mac whole for 10 minutes for no verdict
  ([#217](https://github.com/vocaro/queuekeeper/issues/217)).
- **Bisects.** A bisect's older half is offered to the farm like any gate, so it too goes to an unlocked
  Mac ([the merge queue](merge-queue.md), *A bisect's two halves gate at once*).
- **`"display": true` is no substitute.** It wakes the display and keeps it on, behind a lock too, but
  unlocks nothing. A `"display": true` lane whose tests report a locked screen as unmeasured also says
  `"unlockedScreen": true`; one whose tests measure behind a lock says `"unlockedScreen": false`.
  `qk doctor` warns of a `"display": true` lane that leaves it out. Without it, the merge queue once sent
  a gate to a locked Mac three times in a row until the queue held
  ([vocaro-engineering#293](https://github.com/vocaro/vocaro-engineering/issues/293)).

For example, Pagepour's gate and its Mac lanes `mac-tests` and `mac-purchases` declare it, since they
read their tree through the accessibility API until their tests move to in-process NSAccessibility
methods ([Pagepour#123](https://github.com/vocaro/Pagepour/issues/123)).

## Screen readings in logs and receipts

- **On admission.** A lane that says `"unlockedScreen": true` or `"display": true` has the screen of the
  Mac that runs it read and logged, `queuekeeper: screen: STATE (...) on this Mac (NAME) at HH:MM:SS` (or
  on another Mac), and kept in its receipt as `screen`.
- **When it ends unmeasured**, the screen is read again as `screenAfter`, after
  `queuekeeper: LANE unmeasured on this Mac (NAME)`. Any lane that ends unmeasured with a log saying the
  screen is locked is read again the same way.
- **In the merge queue.** The hold for runs that gave no verdict names the Mac the lane last ran on and
  quotes the last screen line of its log. A run whose last reading shows a locked screen does not count
  toward that hold, and the queue runs the lane next with `qk run --unlocked-screen`, which places even
  an undeclared lane as `"unlockedScreen": true` does
  ([the merge queue](merge-queue.md#the-merge-queue), vocaro-engineering#293).
- **A waiting gate's place.** A waiting lane that needs an unlocked screen has a place on a Mac only
  while that Mac's screen reads unlocked ([heavy-work admission](admission.md#heavy-work-admission),
  [vocaro/queuekeeper#214](https://github.com/vocaro/queuekeeper/issues/214)).
