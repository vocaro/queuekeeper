# The agent interface

Queuekeeper's users are mostly AI agents, so its output is written to be acted on without a person
reading it.

## Exit codes

Exit codes separate the cases an agent must treat differently:

| Code | Meaning |
|---|---|
| 0 | Passed, or landed |
| 1 | Failed, or came back |
| 2 | Usage or configuration error |
| 3 | Unmeasured |
| 4 | Failed before any test started |
| 75 | Busy or held; nothing ran |
| 78 | A host is misconfigured |

## Output

- **Fixed status lines** end every run with the verdict, the test counts, up to twenty failing
  tests with file and line, and the path of the full log.
- **Refusals** say what did not run and the command to run next.
- **JSON** is available from `queue status`, `queue doctor`, `queue placement`, `queue events`,
  `admissions now` and `doctor`.
- **Nothing prompts.** Commands are meant to run in the background and be followed to their result.

## Holds

When the queue stops, the hold's reason begins with whom it is for. Most holds are for an agent,
which fixes the cause and runs `queuekeeper queue resume` itself. A hold is for the owner only when
an agent cannot fix the cause, such as a refused SSH key. With the
[notify feature](visibility.md#the-notify-feature) on, the owner is told of such a hold when it
happens.

See also [The merge queue](merge_queue.md).
