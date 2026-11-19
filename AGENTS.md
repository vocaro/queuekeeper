# Working on Queuekeeper

Read [the user guide's agent section](doc/user_guide.md#for-agents) for exit codes, verification, landing and recovery,
and [the developer guide](doc/developer_guide.md) for how Queuekeeper works inside.
Queuekeeper requires macOS and Python 3.14 or newer; its Python package has no runtime dependencies.
Use this checkout's launcher, `python3 scripts/qk`, to operate on this repository.
Edit Queuekeeper only in a linked worktree; never switch branches or leave edits in the shared checkout,
`~/Development/queuekeeper`, which every project's launcher runs from. `qk` and `qk doctor` say when it is off `main`.

## Verify through admission

Run focused tests with:

```sh
python3 scripts/qk unittest -s tests integration.test_merge_queue.Landing
```

Replace the example with the relevant module, class or test. Run the configured full gate with
`python3 scripts/qk run gate`. Every test or build must take admission, even a single test:
use `python3 scripts/qk admit -- COMMAND` for other build commands. Do not run bare
`python3 -m unittest`, `swift test` or `xcodebuild`. Use `--unadmitted` only when testing a runner
itself, as described in [the admission guide](doc/guides/shared-host.md#heavy-work-admission).
Run `python3 tools/dashboard-screenshots.py`, which retakes the README's screenshots with this Mac's Chrome,
directly and not through admission ([why](doc/guides/maintenance.md#the-readmes-screenshots)).

## Land and recover

When landing work, commit on your own branch and run `python3 scripts/qk land --wait --json` in the
background, following it to its result. Never push directly to `main`. Exit 0 means landed,
1 means refused or sent back, and 75 means held or still waiting at the limit.
Pieces that build on each other ride one gate: give each a branch holding the ones before it, land
them all in order without waiting, and follow them with `python3 scripts/qk queue wait ID ID --json`
([Changes that build on each other](doc/user_guide.md#changes-that-build-on-each-other)).

Interrupting the wait does not withdraw the entry. After interruption or settlement, retrieve its
record with `python3 scripts/qk queue result ID --json`; use this record for the verdict, failures
and artifact locations, since a terminal's last lines may omit them. To keep waiting, use
`python3 scripts/qk queue wait ID --json`, or repeat the same `land --wait --json` to follow the
existing entry. Omit `--json` for text output. Check the result before submitting replacement work.
For complete log output, use `python3 scripts/qk queue logs ID --json` and continue with its
`nextCursor` through `--cursor CURSOR`. Retain each chunk; a terminal tail is not the full log.
Use `queue status` and `queue doctor` to investigate holds; fix and resume a session's hold yourself,
and involve the owner when the hold says it requires the owner.

## Stay in your repository and keep its evidence

Work in another repository is done by a session started there. A change spread across repositories runs
as one session per repository, in order; each lands its part and leaves the next a hand-over note.
Never create a worktree in, write in or land in a repository this session was not started in, even if
the brief asks; if it asks, say so before starting.

`git worktree remove` deletes ignored files without warning, with or without `--force`. Run it by hand
only on a worktree proved to hold no ignored files except caches. Any other worktree goes through the
project's documented disposal plan, such as a closeout cleanup script, even when the owner said to
remove it, unless the owner named the files. Results, receipts, logs and captures under build folders
are evidence, not caches.

## Find the implementation

- `src/python/queuekeeper/cli.py`: command dispatch and lane execution.
- `src/python/queuekeeper/merge_queue.py`: submission, batching, workers and landing.
- `src/python/queuekeeper/agent_cli.py`, `agent_results.py`, `agent_logs.py` and `recovery.py`:
  structured results, resumable log reads and recovery commands.
- `src/python/queuekeeper/config.py` and `schemas/queuekeeper.schema.json`: project settings;
  keep the parser and schema consistent.
- `src/python/queuekeeper/extensions.py`: extension hooks. Keep platform behavior in its extension;
  the core does not import extensions directly.
- `src/python/queuekeeper/visibility/`: optional visibility features.
- `plugins/`: separately packaged platform and visibility plugins; `queuekeeper-workspace.json` declares checkout composition.
- `tests/`: component suites, `integration/`, architecture checks, and `test_support/` fixtures. Follow neighboring tests' `host_isolation` setup.
- `doc/user_guide.md` and `doc/developer_guide.md`: the two main documents; `doc/guides/`: reference detail and design decisions.

See [maintenance](doc/guides/maintenance.md) for queue operation and spelling rules.
