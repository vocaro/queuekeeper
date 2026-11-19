# Related tools

Several good merge queues exist, and for most teams one of them is the right choice. This page
compares them with Queuekeeper and says where each is the better fit.

You may not need a new queue at all. A hosted queue can already drive your own Mac. Mergify's and
Trunk's free tiers cover a solo developer's private repositories, and both work with a GitHub Actions
self-hosted runner on a Mac. GitHub's own queue cannot do this for a personal account: it needs an
organization-owned repository, and the enterprise plan when that repository is private. GitLab
documents Mac runners for iOS work, with merge trains on its paid tiers.

That setup still goes through pull requests and a CI service, and the queue knows nothing about the
Mac it runs on. The gaps Queuekeeper fills are:

- admission that counts everything the agents run, not only gates;
- placement across Macs by measured load;
- a screen-lock check, so a locked Mac is not mistaken for a failing test;
- the [Apple plugin](../plugins/apple/src/queuekeeper_apple/README.md)'s simulator pool and device
  reservations.

The facts below were read from each tool's own documentation in November 2026. Follow the links to
check for changes. "Not documented" means the documentation does not mention it. Prices are
described only in general terms because they change often.

## Hosted and team merge queues

| | Queuekeeper | [GitHub merge queue][gh] | [Mergify][mergify] | [GitLab merge trains][gitlab] | [Trunk][trunk] | [Aviator][aviator] | [Graphite][graphite] |
|---|---|---|---|---|---|---|---|
| **Runs where** | Your Macs | GitHub | SaaS; self-hosted on the top plan | GitLab.com or self-managed | SaaS | SaaS; self-hosted on the top plan | SaaS |
| **Code host** | Any Git remote | GitHub, organization repositories | GitHub | GitLab | GitHub; GitLab.com in beta | GitHub | GitHub, organization repositories |
| **What you queue** | Commits and branches | Pull requests | Pull requests | Merge requests | Pull requests | Pull requests | Pull requests |
| **Code review, approvals** | None | Yes | Yes | Yes | Yes | Yes | Yes, with stacked pull requests |
| **Who runs the tests** | Queuekeeper, on your Macs | Your CI | Your CI | GitLab CI | Your CI | Your CI | Your CI |
| **Your own Mac as the builder** | Built in | Through a self-hosted Actions runner | Through your CI's self-hosted runner | Through a GitLab runner, documented for iOS | Through your CI's self-hosted runner | Through your CI's self-hosted runner | Through your CI's self-hosted runner |
| **Batching** | Yes: everything waiting is one batch | Merge groups; each build still runs | Yes | No | Yes | Yes | Private beta |
| **Speculative parallel checks** | One gate stacked on the running one | Yes, many at once | Yes, many at once | Yes, many at once | Yes | Yes | Yes |
| **Finding the bad change in a batch** | Bisects, both halves at once; skips the bisect when the failure points at one entry | Removes the failing pull request and rebuilds | Bisects | Removes the failing merge request and restarts | Bisects | Bisects | Isolates or bisects |
| **Flaky tests** | Reruns the failed tests alone; lands a proven flake and records it (Python test lanes only) | Not documented | Later batches can vouch for earlier ones; quarantine in a separate product | Not documented | Optimistic merging; retries failures that match flaky patterns | Classifies a failure and reruns the job once (beta) | Not documented |
| **Priorities** | Jump to the front | Jump to the front | Priority rules | Not documented | Numbered levels | Yes | Fast-track |
| **Splitting by affected paths** | Path globs choose gate lanes; an unmatched path runs them all | No | Scopes, with Bazel and Nx | No | Parallel queues, with Bazel and Nx | Affected targets, with Bazel and Nx | Not documented |
| **Pause** | Holds itself with a reason; no manual pause | Not documented | Pause and scheduled freeze | Not documented | Pause, drain, resume | Pause per repository or branch | Pause |
| **Simulators, devices, Xcode** | Apple plugin | Not documented | Not documented | Runner setup guide only | Not in the queue | Not documented | Not documented |
| **Admission by machine load** | Yes | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI |
| **Interface** | CLI with exit codes and JSON | Web, `gh`, API | Web, CLI, API, agent skills | Web, API | Web, CLI, API | Web, API | Web, CLI |
| **Price** | Free, Apache 2.0 | Included with GitHub; private repositories need the enterprise plan | Free tier for small teams; paid per seat | Paid tiers only | Free tier for small teams | Paid per developer | Paid per user, on the higher plans |

[gh]: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
[mergify]: https://docs.mergify.com/merge-queue/
[gitlab]: https://docs.gitlab.com/ci/pipelines/merge_trains/
[trunk]: https://docs.trunk.io/merge-queue
[aviator]: https://docs.aviator.co/mergequeue
[graphite]: https://graphite.com/docs/graphite-merge-queue

Also worth knowing:

- [Zuul](https://zuul-ci.org/) gates changes across many repositories at very large scale. It is
  heavy for one person to run.
- [bors-ng](https://github.com/bors-ng/bors-ng), the original batching queue, was archived in 2024.
- [Kodiak](https://github.com/chdsbd/kodiak) and [Bulldozer](https://github.com/palantir/bulldozer)
  merge pull requests automatically but test nothing themselves.

## Local merge queues for AI agents

Queuekeeper is not the only local merge queue written for AI coding agents. The tools below run a
check command on your own machine instead of in a hosted CI service. The table gives what each
tool's source and documentation showed in November 2026. None was installed or run for this comparison.

| Tool | What it does |
|---|---|
| [merge-queue](https://github.com/chlwys/merge-queue) | Local Git queue for Claude Code and Codex agents in worktrees. Queues committed HEADs one at a time, first in, first out, plus an optional fast lane. Picks checks by changed path and refuses a change no rule covers. Fails a check that changes the candidate. Lands with a local fast-forward and does not push. MCP server, CLI with JSON output and defined exit codes, a SessionStart hook that reports failed requests, macOS menu-bar monitor. Python with runtime dependencies. MIT. One machine. |
| [mergetrain](https://github.com/yongjip/mergetrain) | Local queue for parallel agents on macOS, Linux and Windows. Queues exact commits. Batch or ordered mode, shell-command gates, gates by changed path. Bisects a failed train and, when both halves pass, finds the smallest set of entries that fail together and names each one's partner. Atomic push with recovery of an interrupted push. CLI, JSON, MCP, a hub across repositories. State in SQLite. MIT. One operator, no remote runners. |
| [claude-code-merge-queue](https://github.com/funador/claude-code-merge-queue) | A first-in, first-out lock rather than a queue: `land` rebases and pushes from the agent's own worktree, and one check command runs in a pre-push hook. One `init` command writes the Claude Code hooks, the CLAUDE.md block and the pre-push hook, and removes them again. Prunes landed worktrees. MIT. One machine. |
| [jjq](https://github.com/paulsmith/jjq) | Local merge queue for Jujutsu repositories, aimed at agents. One check command, one item at a time, or several speculative checks with the merge strategy. Moves a local bookmark and does not push. No daemon: someone runs the queue. Reject with a reason, JSON status, an agent skill. BSD-2-Clause. |
| [SelfCI](https://app.radicle.xyz/nodes/radicle.dpc.pw/rad:z2tDzYbAXxTQEKTGFVwiJPajkbeDU) | Local-first CI for Git and Jujutsu with a merge queue daemon that checks one candidate at a time and moves the local branch. Jobs and steps started from a script, deadlines that warn before they fail, and deadlines relative to past run times. Support for several machines is planned. MPL-2.0. |
| [sluice](https://github.com/boomdrivedev/sluice) | Merge queue for agents in worktrees, for macOS and Linux. Tests each submission on top of those queued ahead, several at once or in batches, and blames one culprit. A Git hook and a Claude Code hook stop sessions from moving protected branches. Optional Seatbelt sandbox, reuse of earlier results, and hosts that share one queue through the Git remote. MIT. |

They are simpler to set up than Queuekeeper, and several offer an MCP server or hooks that wire
themselves into Claude Code, which Queuekeeper does not. Most run on one machine and gate one change
at a time. mergetrain bisects, and sluice tests several changes at once across hosts, but none admits
work by machine load or knows about simulators or devices. If one machine and one check command are
enough, start with one of them.

## Where the others are better

- **Teams.** Reviews, approvals, branch protection, single sign-on, audit trails and dashboards a
  whole team shares. Queuekeeper has none of these and assumes everyone who can queue a commit is
  trusted. Its [dashboard](user_guide.md#the-dashboard) is one owner's, read-only and served to their own Mac.
- **Throughput.** The hosted queues test many speculative batches at once across as many CI
  machines as you pay for. Queuekeeper runs one gate, one gate stacked on it, and the two halves of
  a bisect, on a handful of Macs.
- **Large monorepos.** Mergify, Trunk and Aviator split a queue by affected targets and integrate
  with Bazel and Nx. Queuekeeper chooses gate lanes by path globs, with no build graph, and still
  runs one queue.
- **Clean, elastic builders.** A hosted runner starts fresh every time and never depends on a desk
  Mac being awake, logged in and unlocked.
- **Flaky-test history.** Trunk, Mergify and Aviator track flaky tests across runs and can
  quarantine them. Queuekeeper proves a flake by rerunning it, for one kind of lane.
