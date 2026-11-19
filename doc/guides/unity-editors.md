# Unity editors

A project that lists the [Unity plugin](../../plugins/unity/src/queuekeeper_unity/README.md) runs its editor
unattended. This guide says what several sessions on one Mac share, and what the plugin does
about it.

## What is separate for each project path

Unity's exclusions are per project path, and a Git worktree is a different path. So two worktrees
of one repository hold two separate Unity projects, and each may have its own editor open.

| State | Scope |
|---|---|
| The project lock, `Temp/UnityLockfile` | Per project path. A second editor on the same path is refused; editors on two worktrees are normal |
| `Library/`, `Temp/`, `Logs/` and `UserSettings/` | Per project path. Each worktree makes its own |
| The Pipeline package's server and token, in `Library/Pipeline/` | Per project path. Each editor takes its own port |

The cost is the cold `Library/`. A new worktree's first editor imports every asset and compiles
every shader, which takes minutes and several gigabytes. Keep a worktree and use it again, and let
`qk disk` reclaim the folders of the ones that are idle.

## What is shared

- **The GPU.** Every editor and batch run on a Mac uses one device. Results computed from state
  stay valid while runs overlap, but a time measured while another run uses the GPU is void. Run a
  measurement in an exclusive lane.
- **An editor in Play mode.** It keeps simulating while unfocused, and admission cannot see it. An
  exclusive `"unity": true` lane ends unmeasured while one plays on that Mac.
  `qk host editors` shows which.
- **The licensing helper.** One helper serves a user's editors. The last editor to close can leave
  it alive but unanswering, and the next editor then waits 60 seconds and fails. The plugin stops
  the helper when no editor is running, before it starts one.
- **A push.** A pre-push hook that runs the project's tests starts a batch editor. A push during
  another session's measurement loads the GPU as an open editor does, so ask that session to hold
  its pushes too.

## Running a batch while an editor is open

A batch run needs the project lock, which an open editor holds.

```sh
python3 scripts/qk unity closed -- ./tools/verification/check.sh --gates
```

This closes the project's editor with SIGTERM, waits for it to exit, runs the command, and opens
the editor again. It never force-kills. An editor that stays open, for a prompt about unsaved
changes, ends the run unmeasured with nothing run.

A lane that runs the editor says `"unity": true`. Queuekeeper then checks, on the Mac that runs the
lane, that a batch run can start. When it cannot, the lane ends unmeasured, and the merge queue
tries it again and then holds. No commit is sent back as failed for it.

## Rules the plugin keeps

- **Match an editor by its absolute project path.** A short fragment such as `mpm/lab` also matches
  the main checkout's editor from a worktree. That once closed another session's editor and opened
  it again on the wrong project (vocaro/mpm, 2026-08-15).
- **Judge the lock by who holds it open.** A closed editor leaves `Temp/UnityLockfile` behind. The
  plugin removes it only when `lsof` shows that no process holds it, and leaves it when `lsof`
  cannot answer or the path uses a symbolic link.
- **Use the editor the project pins.** A newer editor reimports the `Library/` and rewrites
  `ProjectSettings/ProjectVersion.txt`. `qk doctor` warns when a Mac lacks the pinned version.
- **End a silent run.** A batch run whose log has not been written for ten minutes is ended with
  its import workers, since a stuck run keeps the lock.
- **Never pass `-nographics`.** Without a graphics device Unity does not load compute shaders.

## A second session's checklist

1. Create the worktree. Open its editor with `python3 scripts/qk unity open --ensure` when it is
   needed, and expect a cold import the first time.
2. Run `python3 scripts/qk host editors` before driving any editor, to see which are open and
   which is in Play mode.
3. Keep at most one editor in Play mode.
4. Run measurements alone, in an exclusive lane.
