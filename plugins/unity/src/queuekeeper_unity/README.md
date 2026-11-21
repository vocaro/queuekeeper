# The Unity plugin

The Unity plugin runs a Unity project's editor on a shared Mac without a person watching it. It
builds on the [Mac desktop plugin](../../../mac-desktop/src/queuekeeper_mac_desktop/README.md), so a project that lists it gets that
one too.

Switch it on in `queuekeeper.json`, and say where the Unity project is:

```json
{
  "plugins": ["queuekeeper_unity"],
  "unity": {"project": "lab", "layout": "tools/unity/layouts/worktree-default.dwlt"}
}
```

| Key | Meaning |
|---|---|
| `project` | The folder that holds `ProjectSettings`, relative to the checkout. The checkout itself when left out |
| `layout` | A window layout (`.dwlt`) that `qk unity open` gives a worktree with none of its own |

## What it handles

| Problem | What the plugin does |
|---|---|
| A newer editor installed beside the pinned one reimports the Library and rewrites `ProjectVersion.txt` | Picks the version the project pins. A Mac without it falls back to its newest editor, with a warning |
| A closed editor leaves `Temp/UnityLockfile` behind | Removes it only when `lsof` shows no process holds it open |
| The last editor leaves its licensing helper alive but unanswering, so the next editor waits 60 seconds and fails | Stops that helper when no editor is running. It is never force-killed |
| A batch run stalls and keeps the project's lock | Ends a run whose log has been silent for ten minutes, with its import workers |
| A batch run cannot start while the editor has the project open | `qk unity closed` closes the editor, runs the command, and opens the editor again |

Editors are matched by the absolute project path they were started with, so a sibling worktree's
editor is never closed or counted as this project's.

## Lanes

A lane whose command runs the editor says so:

```json
"gate": {"command": ["./tools/verification/check.sh", "--gates"], "resource": "exclusive", "unity": true}
```

Queuekeeper then runs the command under `qk unity ready --` on whichever Mac runs the lane.
The lane ends unmeasured, with nothing run, when that Mac has no editor, an editor has the project
open, the lock is held, or an orphaned licensing helper will not stop. A lane without the key, such
as a docs check, is not stopped by an open editor.

An exclusive lane measures, and admission holds off only admitted work. An editor left in Play mode
keeps simulating on the GPU while it is unfocused. So a `"unity": true` lane whose `resource` is
`exclusive` also ends unmeasured while any editor on that Mac, on any project, is in Play mode.
A paused editor, or one whose Play state cannot be read, does not stop it.

For a lane that needs a licensed editor, add `"labels": ["unity-license"]` to the lane. Give that
label only to Macs where an admitted Unity batch run has obtained a license in the same session
context the lane uses. Set it in each Mac's `mac-settings.json` as described under
[What a Mac takes](../../../../doc/user_guide.md#what-a-mac-takes). Placement leaves Macs without the
label out. A lane whose results differ by GPU can require both `unity-license` and a chip label,
such as `apple-m5-max`.

A command under bare `qk admit` can go to another Mac without the lane's labels. For a one-off
Unity command, use `qk admit --here -- COMMAND` on a Mac where licensing has been verified.

## Flaky tests

When a gate lane whose command is `qk unity test` fails, the merge queue reruns its failed tests
alone before a bisect, as it does a `qk unittest` lane's (the user guide's
[When a batch fails](../../../../doc/user_guide.md#when-a-batch-fails)):

1. The plugin names each test case that failed or erred in the `results.xml` the run kept, by its
   full name. More than five failed tests, or none named, bisect at once.
2. Each is rerun alone, up to three times, with the lane's own command and `--filter TEST`, which
   passes Unity's `-testFilter` the name escaped and anchored. A filtered run whose results do not
   hold that test exits 3 and counts as a failed rerun.
3. A test that fails again alone is a real failure, and the batch is bisected. A test that passes
   every rerun is a flake, recorded as a Python one is: on the entries, in a `Flaky-test:`
   trailer and on the tracker.
4. The batch lands on its flakes only if it changes none of the paths the lane's tests depend on,
   which the lane lists in `failedTests`:

```json
"gate": {
  "command": ["python3", "scripts/qk", "unity", "test", "--platform", "PlayMode"],
  "unity": true,
  "failedTests": {"paths": ["lab/Assets/", "lab/Packages/", "lab/ProjectSettings/"]}
}
```

A lane that lists no paths still has its flakes recorded, and is then bisected. The rerun keeps
the lane's `--platform` and `--unadmitted`. A lane that gives `--results`, or runs Unity through a
script of its own, gets no reruns from the plugin; such a script can declare its own `failedTests`
with a JUnit XML file and a rerun command.

## Doctor, disk and host

- `qk doctor` warns when the pinned editor is missing on this Mac or another Mac, when
  Unity Hub is signed out on this Mac, when a lane passes `-nographics`, and when there is no Unity
  project where the project says.
- `qk disk` lists `Library/`, `Temp/` and `Logs/` in every checkout of the project, and
  `qk disk --delete` removes them. They are kept while an editor has the project open, while they
  changed lately, or unless the project's `.gitignore` ignores them and nothing in them is tracked.
- `qk host editors` lists the editors and batch runs on this Mac, each with its project
  and version, whether an editor is in Play mode, and the versions installed.

The sign-in and the Play state are read with Unity's own command, `unity`, which Unity Hub 3.21 and
later installs at `~/.unity/bin/unity`. An editor answers for its Play state only when its project
lists the Pipeline package (`com.unity.pipeline`). Without the command or the package, the sign-in
or the Play state is unknown, which is never taken for signed out or for playing.

## Commands

```bash
python3 scripts/qk unity test
```

Runs the project's tests in a batch editor (`--platform PlayMode` for Play mode tests). It reports
the counts, each test that failed, and compile errors. It never passes `-nographics`: without a
graphics device Unity does not load compute shaders.

In a lane it keeps Unity's `results.xml` and `tests.log` under `unity/` in the lane's results
folder. When the lane ends, `qk run` prints a line for each test with its outcome and seconds, then
the totals, as it does for the Apple plugin's test runs:

```
unity: passed 0.012s Lab.Grid.Wraps
unity: failed 1.500s Lab.Grid.Clamps (Expected: 4)
unity: 1 passed, 1 failed
```

`--filter TEST` runs only the test whose full name is TEST, as the merge queue reruns a failed
test alone ([Flaky tests](#flaky-tests)), and exits 3 when no such test ran.

Started outside a lane, it is admitted and placed first, as `qk admit` would run it.
`--unadmitted` after `test` runs it with no admission, on this Mac.

```bash
python3 scripts/qk unity closed -- ./tools/verification/check.sh --gates
```

Closes the project's editor with SIGTERM, runs the command, and opens the editor again if it closed
one. An editor that stays open, such as one asking about unsaved changes, is left alone and nothing
runs.

```bash
python3 scripts/qk unity ready -- ./tools/verification/check.sh --gates
```

Runs the command once a batch run may start on the project, and exits 3 with nothing run when one
may not. This is what a `"unity": true` lane runs under. `--exclusive` before the `--` also exits 3
while an editor on this Mac is in Play mode, as an exclusive lane does.

```bash
python3 scripts/qk unity open
```

Opens a linked worktree's editor once, for a `post-checkout` hook. It does nothing in the main
checkout or after the first time. `--ensure` opens it now, and `--status` only says whether one is
open. A session that needs no editor sets `QUEUEKEEPER_UNITY_OPEN=0`.

```bash
UNITY_BIN="$(python3 scripts/qk unity editor)"
python3 scripts/qk unity run --log gate.log -- "$UNITY_BIN" -batchmode -quit -projectPath "$PWD/lab" -executeMethod Gates.Run -logFile gate.log
```

For a script that starts Unity itself, as one that runs a project's own gates. `editor` prints the
binary of the editor the project pins, alone on standard output. `run` runs a batch run and ends it,
with its import workers, when its log has been silent for ten minutes. It exits with the run's own
status, or 124 for a run it ended. Such a script checks first that a run may start, with
`python3 scripts/qk unity ready -- true`.

## Exit statuses

| Status | Meaning |
|---|---|
| 0 | The tests passed, or the wrapped command did |
| 1 | A test failed, the project did not compile, or the run wrote no results |
| 2 | The command was misused, or the project has no Unity project where it says |
| 124 | `run` ended a batch run whose log went silent |
| 3 | Unmeasured: nothing ran. No editor on this Mac, an editor holding the project, a held lock, a licensing helper that would not stop, an editor in Play mode during exclusive work, a run that hung, or a `--filter` that ran no such test |

The merge queue tries an unmeasured run again and then holds it. It does not send the commit back as
failed. `closed` and `run` otherwise exit with their command's own status.

## Settings

| Variable | Meaning |
|---|---|
| `UNITY` | An editor's binary, used instead of the hub's |
| `UNITY_CLI` | Unity's `unity` command, used instead of `~/.unity/bin/unity` |
| `QUEUEKEEPER_UNITY_QUIET` | The seconds a batch run's log may stay unwritten before the run is ended (default 600) |
| `QUEUEKEEPER_UNITY_OPEN` | `0` stops `qk unity open` from opening an editor by itself |

## Limits

- **Play mode needs the Pipeline package.** Only the editor knows its Play state, and it answers
  through that package. An editor on a project without it is never counted as playing. The reply's
  form was not read from a live editor when this was written (2026-11-12): the plugin looks for
  `playMode` anywhere in it.
- **Hub sign-in is advisory.** Over SSH, Unity's command may not read the keychain that holds
  the Hub session. Use an admitted batch run to verify editor licensing. If `qk unity test` gets a
  licensing initialization error in place of test results, it ends unmeasured.
- **Running many gates in one editor** is not here. vocaro/mpm's shared gate runner needs a Unity
  package for its C# half, and stays there until a second Unity project needs it
  ([issue 6](https://github.com/vocaro/queuekeeper/issues/6)).
- **Whether a batch run with a graphics device works over SSH** on another Mac, or needs an
  unlocked screen, is not tested.

The user guide's [Unity editors](../../../../doc/user_guide.md#unity-editors) section
says what two sessions share on one Mac and what they do not.
