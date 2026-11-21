# Working on Queuekeeper

Read [the user guide's agent section](doc/user_guide.md#for-agents) for exit codes, verification, landing and recovery,
and [the developer guide](doc/developer_guide.md) for how Queuekeeper works inside.
Queuekeeper requires macOS and Python 3.14 or newer. Its Python package has no runtime dependencies.
Use this checkout's launcher, `python3 scripts/qk`, to operate on this repository.
Edit Queuekeeper only in a linked worktree. Keep the shared Queuekeeper checkout clean and on `main`
because other projects may load Queuekeeper from it. See [working on Queuekeeper while it is in use](doc/developer_guide.md#changing-queuekeeper-while-work-runs).

## Claim before you take work

Before taking an issue, read its comments for an active claim, then claim it with a short comment naming your
session by title and id and its branch; update that comment when you release or finish the work. Read the claims
on every issue it links to as well, and look in `python3 scripts/qk queue status` and `git worktree list` for an
entry or branch naming the same issue: a claim on one issue is not seen by a session working a related one.
A coordinator that sends several subagents at once gives each a distinct file prefix or folder in the session
scratchpad, which isolated subagents still share, and names it in the brief.

These rules repeat the owner's shared rules in vocaro-engineering's `doc/agents/core.md`, which a session
here does not load; change them there first. Rules about how sessions work across every project,
such as when a session may land without asking, belong there and are not repeated here.

## Verify through admission

Run focused tests with:

```sh
python3 scripts/qk unittest -s tests integration.test_merge_queue.Landing
```

Replace the example with the relevant module, class or test. Run the configured full gate with
`python3 scripts/qk run gate`. Every test or build must take admission, even a single test:
use `python3 scripts/qk admit -- COMMAND` for other build commands. Do not run bare
`python3 -m unittest`, `swift test` or `xcodebuild`. Use `--unadmitted` only when testing a runner
itself, as described in [the user guide's admission section](doc/user_guide.md#admission).
When a change affects how the dashboard looks, retake the README's screenshots by running
`python3 tools/dashboard-screenshots.py` directly. The script uses local Chrome and writes to this
checkout. See [the screenshot instructions](doc/developer_guide.md#the-readmes-screenshots).

No hosted runner runs Queuekeeper's tests or builds: the merge queue's gate on the owner's Macs is its CI.
Add no GitHub Actions or other hosted-runner workflow that runs them, not even for outside pull requests,
which a maintainer lands through the queue ([why](doc/developer_guide.md#no-hosted-ci)).

## Land and recover

When landing work, commit on your own branch and run `python3 scripts/qk land --wait --json` in the
background, following it to its result. Never push directly to `main`. Exit 0 means landed,
1 means refused or sent back, and 75 means held or still waiting at the limit.
Pieces that build on each other ride one gate: give each a branch holding the ones before it, land
them all in order without waiting, and follow them with `python3 scripts/qk queue wait ID ID --json`
([Changes that build on each other](doc/user_guide.md#changes-that-build-on-each-other)).

Name the issue a commit works on in its message so that GitHub acts on it when the commit lands.
Write `Closes #N` on a line of its own when the commit meets everything the issue's "done when" asks,
so that GitHub closes the issue as the queue pushes to `main`. Write `Refs #N` when work is left,
and say on the issue what remains. A reference such as `(vocaro/queuekeeper#N)` in the subject
closes nothing, and the queue refuses a negated claim such as "does not fix #N", which GitHub would
still act on.

Interrupting the wait does not withdraw the entry. After interruption or settlement, retrieve its
record with `python3 scripts/qk queue result ID --json`; use this record for the verdict, failures
and artifact locations, since a terminal's last lines may omit them. To keep waiting, use
`python3 scripts/qk queue wait ID --json`, or repeat the same `land --wait --json` to follow the
existing entry. Omit `--json` for text output. A successful `queue result` exit confirms the lookup.
Check `state` and `terminal` in the result before submitting replacement work.
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
- `src/python/queuekeeper/agent_setup.py` and `agent_hooks.py`: `qk agents` and the Claude Code hooks it installs.
- `src/python/queuekeeper/config.py` and `schemas/queuekeeper.schema.json`: project settings;
  keep the parser and schema consistent.
- `src/python/queuekeeper/plugins.py`: plugin hooks. Keep platform behavior in its plugin;
  the core does not import plugins directly.
- `src/python/queuekeeper/visibility/`: optional visibility features.
- `plugins/`: separately packaged platform and visibility plugins; `queuekeeper-workspace.json` declares checkout composition.
- `tests/`: component suites, `integration/`, architecture checks, and `test_support/` fixtures. Follow neighboring tests' `host_isolation` setup.
- `doc/user_guide.md` and `doc/developer_guide.md`: the documentation, for users and for people changing Queuekeeper.

See [the merge queue](doc/developer_guide.md#the-merge-queue) for queue operation and [spelling](doc/developer_guide.md#spelling) for spelling.

<!-- queuekeeper:agents begin sha256=7574de43a2ad6c89 -- written by `qk agents setup`; `qk agents remove` takes it out -->
Never push to main. Commit on your own branch, then run `qk land --wait` as a
background command and follow it to its result. Exit 0 means landed, 1 means it came back
with a reason, and 75 means the queue is held or still waiting. If the wait is interrupted, the
entry still lands: follow it with `qk queue wait ID`, or run the same `qk land --wait` again,
which follows the entry already queued. Run tests and builds with `qk run LANE` or
`qk admit -- COMMAND`, never bare, so that they wait their turn on the shared Macs. Run unittest
suites with `qk unittest`, never `python3 -m unittest`. Send a long run's output to a file, never
through `tail` or `head`, and trim when reading it; `qk progress` says how far a run has got.
<!-- queuekeeper:agents end -->
