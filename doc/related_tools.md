# Related tools

Several good merge queues exist, and for most teams one of them is the right choice. This page
compares them with Queuekeeper and says where each is the better fit.

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
| **Splitting by affected paths** | Documentation only | No | Scopes, with Bazel and Nx | No | Parallel queues, with Bazel and Nx | Affected targets, with Bazel and Nx | Not documented |
| **Pause** | Holds itself with a reason; no manual pause | Not documented | Pause and scheduled freeze | Not documented | Pause, drain, resume | Pause per repository or branch | Pause |
| **Simulators, devices, Xcode** | Apple plugin | Not documented | Not documented | Runner setup guide only | Not in the queue | Not documented | Not documented |
| **Admission by machine load** | Yes | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI | Left to CI |
| **Interface** | CLI with exit codes and JSON | Web, `gh`, API | Web, CLI, API, agent skills | Web, API | Web, CLI, API | Web, API | Web, CLI |
| **Price** | Free, Apache 2.0 | Included with GitHub; private repositories need the enterprise plan | Free tier for small teams; paid per seat | Paid tiers only | Free tier for small teams | Paid per developer | Paid per user, on the higher plans |
| **Maturity** | One user, its author | Widely used | Widely used | Widely used | Widely used | Widely used | Widely used |

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

Queuekeeper is not the only local merge queue written for AI coding agents. The tools below queue
commits instead of pull requests and run a check command on your machine. The table gives what each
tool's own README or announcement says. None was installed or run for this comparison.

| Tool | What it documents |
|---|---|
| [merge-queue](https://github.com/chlwys/merge-queue) | Local Git queue for Claude Code and Codex agents in worktrees. Queues committed HEADs. Runs policy checks in an isolated candidate worktree. First in, first out, plus a fast lane. MCP server, CLI with JSON output and defined exit codes, macOS menu-bar monitor. MIT. One machine. |
| [mergetrain](https://github.com/Drezzler/mergetrain-pr) | Local queue for parallel agents. Queues exact commits. Batch or ordered mode, shell-command gates, runs that isolate conflicting pairs. CLI, JSON, MCP. State in SQLite. MIT. One operator, no remote runners. |
| [claude-code-merge-queue](https://github.com/funador/claude-code-merge-queue) | Local first-in, first-out queue for Claude Code. One check command in a pre-push hook. MIT. Describes itself as for one machine, not a fleet. |
| [jjq](https://pauladamsmith.com/blog/2026/02/introducing-jjq-a-local-merge-queue-for-jj.html) | Local merge queue for Jujutsu repositories, aimed at agents. One check command, one item at a time. |
| [SelfCI](https://docs.rs/crate/selfci/latest/source/README.md) | Local-first CI with a local merge queue daemon. Support for several machines is planned. |
| [sluice](https://github.com/agentplot/sluice) | Describes itself as a merge queue for agents in worktrees. Not read for this comparison. |

They are much smaller than Queuekeeper and simpler to set up, and several offer an MCP server,
which Queuekeeper does not. As documented, each runs on one machine, and none describes bisection,
flaky-test handling, admission by load, or anything about simulators or devices. If one machine and
one check command are enough, start with one of them.

## Where the others are better

- **Teams.** Reviews, approvals, branch protection, single sign-on, audit trails and dashboards a
  whole team shares. Queuekeeper has none of these and assumes everyone who can queue a commit is
  trusted. Its [dashboard](visibility.md) is one owner's, read-only and served to their own Mac.
- **Throughput.** The hosted queues test many speculative batches at once across as many CI
  machines as you pay for. Queuekeeper runs one gate, one gate stacked on it, and the two halves of
  a bisect, on a handful of Macs.
- **Large monorepos.** Mergify, Trunk and Aviator split a queue by affected targets and integrate
  with Bazel and Nx. Queuekeeper only separates documentation from code.
- **Clean, elastic builders.** A hosted runner starts fresh every time and never depends on a desk
  Mac being awake, logged in and unlocked.
- **Flaky-test history.** Trunk, Mergify and Aviator track flaky tests across runs and can
  quarantine them. Queuekeeper proves a flake by rerunning it, for one kind of lane.
- **Maturity.** They have many users, support and a web interface you manage the queue from.
  Queuekeeper has one user, a command line and a web page that only shows.

## You may not need a new queue

A hosted queue can already drive your own Mac. Mergify's and Trunk's free tiers cover a solo
developer's private repositories, and both work with a GitHub Actions self-hosted runner on a Mac.
GitHub's own queue cannot do this for a personal account: it needs an organization-owned repository,
and the enterprise plan when that repository is private. GitLab documents Mac runners for iOS work,
with merge trains on its paid tiers.

That setup still goes through pull requests and a CI service, and the queue knows nothing about the
Mac it runs on. The gaps Queuekeeper fills are:

- admission that counts everything the agents run, not only gates;
- placement across Macs by measured load;
- a screen-lock check, so a locked Mac is not mistaken for a failing test;
- the [Apple plugin](../src/python/queuekeeper/apple/README.md)'s simulator pool and device
  reservations.
