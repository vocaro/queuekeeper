# Lanes and admission

A lane is a command Queuekeeper runs for you, such as a build or a test suite. Admission decides
when a Mac has room to run it.

## Lanes

A lane is a named command in `queuekeeper.json`, written as an argument list, with a timeout and a
statement of what it needs from the machine:

```json
{
  "lanes": {
    "unit":    {"command": ["qk", "unittest", "-s", "tests"], "timeout": 1800},
    "ui":      {"command": ["scripts/ui-tests.sh"], "timeout": 3600, "unlockedScreen": true},
    "measure": {"command": ["scripts/benchmarks.sh"], "resource": "exclusive"},
    "gate":    {"parallel": ["unit", "ui"], "budgetMinutes": 20}
  },
  "queue": {"lanes": ["gate"]}
}
```

`qk run LANE` runs one, for an agent as for the queue.

- **Receipts.** Each run leaves a log and a receipt that records the commit, the command, the exit
  status, the outcome, the Mac it ran on and how long it waited.
- **Results by commit.** Results of whole-lane runs at a clean commit are kept by commit, so any
  worktree can ask whether that commit already passed a lane.
- **Test counts.** A lane can require a minimum number of executed tests, so a run that silently
  selects nothing fails. Counts are read from unittest, pytest, XCTest and Swift Testing output.
  A lane that picks its tests by name can say `"exactTests": true`, and a whole run must then execute
  exactly its `minimumTests`, so that a test added to or dropped from the lane fails it
  ([test counts](guides/lanes.md#receipts-and-test-counts)).
- **Arguments.** `qk run LANE -- ARGUMENTS` appends the arguments to the lane's command, to run
  some of its tests. A lane whose command selects its own tests says `"arguments": false`, and such
  a run is then refused at once, with exit status 2, before anything is staged or sent to another Mac.
- **Groups.** A lane group runs its members side by side under one admission.
- **Budgets.** `budgetMinutes` warns when the median of the gate's own work, from its admission to its
  end, drifts past the target, and names the slowest parts, the median wait for admission and the median
  landing latency beside it.

## Admission

Ten agents that each start a build at the same moment make every build slow and every timing test
meaningless. Every lane, and any command run through `qk admit -- COMMAND`, therefore asks
the Mac for a slot first, in one first-come, first-served queue for the whole machine.

- Three slots are always available. Further slots, up to half the cores, open one at a time while
  the CPU busy share is under 75% and memory pressure is normal.
- A lane can ask for the whole host, for work that starts its own parallel jobs, or for exclusive
  use, for timing measurements. One lane at a time may use an on-device model or the GPU.
- Exclusive work, once admitted, waits up to five minutes for the CPU to read quiet before its
  command starts, so that the work it waited out does not void its timing, and tells the command
  what it read of the Mac (vocaro/queuekeeper#121).
- A waiting lane says what it is waiting for and behind whom, and repeats it every five minutes.
- `qk admissions now` shows who holds slots and who is waiting on every Mac. An admission
  log records every wait and hold.

Admission cannot preempt running work, and it cannot see work that never asks, such as an editor's
background indexing.

## Process supervision

Every lane runs in its own session and process group with a time limit. When a lane ends, times out
or is stopped, Queuekeeper takes a census of what it started and stops what is left. If something
survives, the run is marked uncertain and its slot stays held until that is settled, so the leftover
load is never hidden. An entry whose owner has provably finished is recovered automatically.

## Checking a project's setup

`qk doctor` checks a project's lanes, such as a lane with no timeout or a test that no lane
runs, and every Mac's setup. Its JSON includes `offload.pythonCrashes`: read-only notes about
`Python-*.ips` reports written during the last 24 hours, on this Mac and each other Mac that
answers the host survey. Each report names its path, capture time, signal, parent process and
first five frames of the crashed thread. Unreadable reports are notes too. These observations
also appear as readable strings in `notes`. They never change the doctor's verdict and do not stop processes or remove reports.

See also [The merge queue](merge_queue.md) and [The build farm](build_farm.md).
