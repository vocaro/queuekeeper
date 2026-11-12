# The Unity plugin

The Unity plugin runs a Unity project's editor on a shared Mac without a person watching it. It
builds on the [Mac desktop plugin](../mac_desktop/README.md), so a project that lists it gets that
one too.

Switch it on in `queuekeeper.json`, and say where the Unity project is:

```json
{
  "extensions": ["queuekeeper.unity"],
  "unity": {"project": "lab", "layout": "tools/unity/layouts/worktree-default.dwlt"}
}
```

| Key | Meaning |
|---|---|
| `project` | The folder that holds `ProjectSettings`, relative to the checkout. The checkout itself when left out |
| `layout` | A window layout (`.dwlt`) that `queuekeeper unity open` gives a worktree with none of its own |

## What it handles

| Problem | What the plugin does |
|---|---|
| A newer editor installed beside the pinned one reimports the Library and rewrites `ProjectVersion.txt` | Picks the version the project pins. A Mac without it falls back to its newest editor, with a warning |
| A closed editor leaves `Temp/UnityLockfile` behind | Removes it only when `lsof` shows no process holds it open |
| The last editor leaves its licensing helper alive but unanswering, so the next editor waits 60 seconds and fails | Stops that helper when no editor is running. It is never force-killed |
| A batch run stalls and keeps the project's lock | Ends a run whose log has been silent for ten minutes, with its import workers |
| A batch run cannot start while the editor has the project open | `queuekeeper unity closed` closes the editor, runs the command, and opens the editor again |

Editors are matched by the absolute project path they were started with, so a sibling worktree's
editor is never closed or counted as this project's.

## Commands

```bash
python3 scripts/qk unity test
```

Runs the project's tests in a batch editor (`--platform PlayMode` for Play mode tests). It reports
the counts, each test that failed, and compile errors. It never passes `-nographics`: without a
graphics device Unity does not load compute shaders.

```bash
python3 scripts/qk unity closed -- ./tools/verification/check.sh --gates
```

Closes the project's editor with SIGTERM, runs the command, and opens the editor again if it closed
one. An editor that stays open, such as one asking about unsaved changes, is left alone and nothing
runs.

```bash
python3 scripts/qk unity open
```

Opens a linked worktree's editor once, for a `post-checkout` hook. It does nothing in the main
checkout or after the first time. `--ensure` opens it now, and `--status` only says whether one is
open. A session that needs no editor sets `QUEUEKEEPER_UNITY_OPEN=0`.

## Exit statuses

| Status | Meaning |
|---|---|
| 0 | The tests passed, or the wrapped command did |
| 1 | A test failed, the project did not compile, or the run wrote no results |
| 2 | The command was misused, or the project has no Unity project where it says |
| 3 | Unmeasured: nothing ran. No editor on this Mac, an editor holding the project, a held lock, a licensing helper that would not stop, or a run that hung |

The merge queue tries an unmeasured run again and then holds it. It does not send the commit back as
failed. `closed` otherwise exits with its command's own status.

## Settings

| Variable | Meaning |
|---|---|
| `UNITY` | An editor's binary, used instead of the hub's |
| `QUEUEKEEPER_UNITY_QUIET` | The seconds a batch run's log may stay unwritten before the run is ended (default 600) |
| `QUEUEKEEPER_UNITY_OPEN` | `0` stops `queuekeeper unity open` from opening an editor by itself |

## Not here yet

These parts of [issue 6](https://github.com/vocaro/queuekeeper/issues/6) are not built:

- Doctor checks for the pinned editor on every Mac, the Hub sign-in and `-nographics` in a lane.
- `Library/`, `Temp/` and `Logs/` in the disk inventory.
- The lock and licensing checks before a lane starts, and an editor in Play mode making an
  exclusive lane unmeasured.
- A Mac's open editors in `queuekeeper host`.
- Placing a lane by the GPU it needs.
- Per-test results kept in the lane's results folder, as the Apple plugin's `xctest` keeps them.
