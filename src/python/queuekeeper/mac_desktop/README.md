# The Mac desktop plugin

The Mac desktop plugin handles a Mac's logged-in session when nobody is at it: the screen, keyboard and
focus a UI test needs, and the dialogs and permissions that stop one. It knows nothing of what is
being built. The plugins that do, such as the [Apple plugin](../apple/README.md), require it, so a
project that lists one of them gets this one too.

A plugin names the plugins it builds on in its module's `REQUIRES`:

```python
REQUIRES = ("queuekeeper.mac_desktop",)
```

Queuekeeper loads those first, once each, so a project's `queuekeeper.json` lists only the plugins
it uses itself.

## What it handles

A UI test on a Mac nobody is watching fails for reasons unrelated to the code. The plugin handles
the ones met so far:

| Problem | What the plugin does |
|---|---|
| Two UI runs fight over one screen and keyboard | Reserves the Mac's foreground for one run at a time |
| A "quit unexpectedly" alert covers the window under test | Suppresses crash dialogs for the run, dismisses any that appear, restores the setting afterward |
| A notification banner covers the screen | Closes it. If it cannot, the run is unmeasured |
| The login keychain is locked in an SSH session, so code signing fails | Unlocks it before each run sent to that Mac, from a password the owner stored once there |
| A test waits for an animation the display's refresh times, and the display is asleep | A lane that says `"display": true` runs under `caffeinate`, which wakes the display and keeps it on |
| macOS asks for screen-capture approval again every 30 days | Launches the test runner from one stable path so the approval persists, and reports when it will lapse |

When a guard cannot clear a problem, the run ends unmeasured instead of failed. The keychain guard
stores a login password on the build Mac, readable only by that account. Use it only on a Mac that
holds nothing sensitive.

## What other plugins add

A plugin that builds on this one can save something before a run takes the foreground and put it
back afterward, by adding a guard to `queuekeeper.mac_desktop.foreground.GUARDS`. The Apple plugin
keeps VoiceOver's state this way. If a guard cannot put back what it changed, the foreground stays
reserved until a person checks the Mac.

A plugin can also add a key to a lane in `queuekeeper.json`, as this one adds `display`: it
registers a `queuekeeper.extensions.LaneKey`, which checks the key's value and can wrap the lane's
command. A project that uses the key without the plugin is told which plugin to list.

## Reservations

The foreground is reserved in one locked record per Mac, which says who holds what and until when.
A plugin that builds on this one keeps its own reservations in the same record, as the Apple plugin
does for physical devices and simulators.
