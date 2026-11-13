"""The warnings about the landing gate's time budget (the toolkit's guides/releasing.md, Keeping the split,
vocaro-engineering#135), which a landing prints after its result and `doctor` gives:

- the gate's median own time, from its admission to its end, over its recent passing runs in the merge queue made on
  an unloaded host exceeds the gate lane's budgetMinutes, naming its median wait for admission beside it, its slowest
  parts and how many loaded runs it left out;
- and, from doctor alone, a direct-main gate with no budget, a gate that takes a physical device (not one whose
  --device options only name simulator types for `qk simulator-tests` or `qk simulator-pool`, vocaro/queuekeeper#113),
  and a device pass's lane that would go to the second Mac.

A project's own tooling adds what else a landing warns of, such as a release gate's commit limit
(extensions.LANDING_WARNINGS).

The queue's worker records each landed entry's gate run (Worker.gate): its wall time as the queue saw
it, wherever the lane ran, any wait for admission included (seconds), and apart from it, since 2026-11-13, the lane's
own work from its admission to its end (workSeconds) and the wait for admission before it (waitSeconds,
merge_queue.Admission, vocaro/queuekeeper#112); the parts its log reports with their times; and the CPU busy share of
the Mac it ran on over its own work, beside the busy limit that Mac sets (cpuBusy and busyLimit,
merge_queue.BusySampler). The budget judges the own work: for a whole-host gate on a shared Mac the wait had become
most of the wall time, and no check moved out of the gate shortens it. A run recorded without workSeconds, before
that date or with its admission unseen, is left out of the median, since its wall time holds a wait the budget no
longer judges. The budget and the owner's under-20-minute goal assume an unloaded machine, so a run whose busy share
was at or above its limit is left out of the median (owner's decision of 2026-11-07, vocaro-engineering#235); a run
recorded without a reading counts. A whole-host gate's run is never left out for that (wholeHost in its record): its
admission keeps other heavy work off the Mac, so the busy share is its own work, and each such gate goes past the limit
by itself in nearly every run (vocaro/queuekeeper#12).
"""
import re
import statistics
import subprocess
from pathlib import Path

from . import extensions

GATE = "gate"
# The passing gate runs the median is taken over, newest first, and the fewest that judge a budget.
RECENT_RUNS = 10
FEWEST_RUNS = 3
# The parts a landed entry keeps, and those a warning names.
KEPT_PARTS = 10
NAMED_PARTS = 5
# What qk unittest and qk xctest print for each process, "== NAME: OK (3 tests, 1.2s)", and what a
# lane group prints for each of its lanes under "Lane times (...)": "  NAME     12s  PASS".
SHARD = re.compile(r"^== (.+?): .*\((?:[^()]*, )?(\d+(?:\.\d+)?)s\)$")
LANE_TIME = re.compile(r"^  (\S+)\s+(\d+(?:\.\d+)?)s\s+(?:PASS|FAIL|UNMEASURED)$")
SPLIT = "the toolkit's guides/releasing.md, Keeping the split"
PASS = "guides/devices.md, The device pass"


def parts(log):
    """The parts a lane's log reports with their times, slowest first, as [name, seconds]."""
    found, grouped = {}, False
    for line in log.splitlines():
        if line.startswith("Lane times ("):
            grouped = True
            continue
        matched = SHARD.match(line) or (LANE_TIME.match(line) if grouped else None)
        if matched: found[matched.group(1)] = found.get(matched.group(1), 0.0) + float(matched.group(2))
        elif grouped and not line.startswith("  "): grouped = False
    return sorted(([name, round(seconds, 1)] for name, seconds in found.items()), key=lambda part: -part[1])


def entries(directory):
    """The merge queue's entries in directory, as Queue.all reads them, without making the queue's folders."""
    import json
    found = []
    for path in sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else []:
        try: found.append(json.loads(path.read_text()))
        except (OSError, ValueError): continue
    return found


def runs(entries):
    """The gate runs that landed entries record, newest first, one per landed commit: a batch's entries
    share the one run that passed them."""
    seen, found = set(), []
    for entry in sorted(entries, key=lambda entry: entry.get("settled", 0), reverse=True):
        run = entry.get("gate")
        # The batch's tip, which every entry of a batch shares; landedAs is each entry's own commit (#184).
        pushed = entry.get("landedTip") or entry.get("landedAs")
        if entry.get("state") != "landed" or not isinstance(run, dict) or pushed in seen: continue
        seen.add(pushed)
        found.append(run)
    return found


def minutes(seconds):
    return f"{seconds / 60:.1f} min"


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def timed(seconds, admitted, began):
    """What a gate run's record keeps of its time: seconds, its wall time as the queue saw it, and, when admitted (an
    epoch time) says when the lane was admitted after it began (an epoch time), waitSeconds before that and
    workSeconds from then to its end (vocaro/queuekeeper#112)."""
    found = {"seconds": round(seconds, 1)}
    if number(admitted) and number(began):
        waited = min(max(admitted - began, 0.0), seconds)
        found.update(workSeconds=round(seconds - waited, 1), waitSeconds=round(waited, 1))
    return found


def own(run):
    """A gate run's own work in seconds, from its admission to its end, or None for a run recorded without it, which
    the budget leaves out (vocaro/queuekeeper#112)."""
    return run["workSeconds"] if number(run.get("workSeconds")) else None


def time_words(run):
    """The worker log's words for a gate run's time (Worker.gate): its wall time, and its own work and its wait for
    admission apart where the record has them."""
    if own(run) is None:
        return (f"{minutes(run['seconds'])}, its admission unseen, so the gate's budget median leaves it out")
    return (f"{minutes(run['seconds'])}: {minutes(run['workSeconds'])} of its own work after "
            f"{minutes(run.get('waitSeconds', 0.0))} waiting for admission")


def loaded(run):
    """Whether a gate run was made on a loaded host: its CPU busy share over the run at or above the busy limit of the
    Mac it ran on, the limit at which admission finds no spare capacity. A run without both readings is not, and
    neither is a whole-host lane's: no other heavy work is admitted beside it, so its busy share is its own work and
    says nothing of the host (vocaro/queuekeeper#12)."""
    if run.get("wholeHost") is True: return False
    return number(run.get("cpuBusy")) and number(run.get("busyLimit")) and run["cpuBusy"] >= run["busyLimit"]


def busy_words(busy):
    """The worker log's words for a gate run's busy share (Worker.gate), saying when the budget leaves it out."""
    if not number(busy.get("cpuBusy")) or not number(busy.get("busyLimit")):
        return "the CPU busy share of the Mac it ran on could not be read"
    return (f"the CPU of {busy.get('busyOn', 'the Mac it ran on')} was {busy['cpuBusy']:g}% busy on average over "
            f"{busy.get('busyReadings', 1)} reading(s), at most {busy.get('cpuBusyHighest', busy['cpuBusy']):g}%, against "
            f"its busy limit of {busy['busyLimit']:g}%"
            + (": a loaded run, which the gate's budget median leaves out" if loaded(busy)
               else ": its own work, as a whole-host lane has the Mac's heavy work to itself, so the gate's budget "
                    "median counts it" if busy.get("wholeHost") is True and busy["cpuBusy"] >= busy["busyLimit"] else ""))


def gate_warnings(config, entries):
    """A warning when the median own work (own) of the gate's RECENT_RUNS latest passing runs made on an unloaded host
    (loaded) exceeds its budget, giving their median wait for admission beside it and saying how many loaded runs newer
    than the oldest of them it left out. A run recorded without its own work is no run here (vocaro/queuekeeper#112).
    None without a budget, or with fewer than FEWEST_RUNS such runs to judge it by. The warning sends the reader to the
    project's open budget proposal, to add to it rather than open another (vocaro/queuekeeper#96)."""
    budget = config.get("lanes", {}).get(GATE, {}).get("budgetMinutes")
    if budget is None: return []
    recent, left_out = [], 0
    for run in runs(entries):
        if len(recent) == RECENT_RUNS: break
        if own(run) is None: continue
        if loaded(run): left_out += 1
        else: recent.append(run)
    if len(recent) < FEWEST_RUNS: return []
    median = statistics.median(own(run) for run in recent)
    if median <= budget * 60: return []
    waits = [run["waitSeconds"] for run in recent if number(run.get("waitSeconds"))]
    waited = (f"; their median wait for admission before it, which the budget leaves out, was "
              f"{minutes(statistics.median(waits))}") if waits else ""
    slowest = next((run["parts"] for run in recent if run.get("parts")), [])[:NAMED_PARTS]
    named = (f"; its slowest parts in its latest run that reports them: "
             + ", ".join(f"{name} {seconds:g}s" for name, seconds in slowest)) if slowest \
        else "; its log reports no parts with their times"
    loads = (f", leaving out {left_out} run{'' if left_out == 1 else 's'} made with the CPU of the Mac it ran on at or "
             f"above its busy limit, as the budget assumes an unloaded host") if left_out else ""
    return [f"gate's median time from its admission to its end over its last {len(recent)} passing runs in the merge "
            f"queue is {minutes(median)}{loads}, over its budget of {budget} min (gate.budgetMinutes){waited}{named}. "
            f"Search the project's issue tracker for its open proposal on the gate's budget and add to it, or open one "
            f"only if there is none, naming which check to move to release.lanes; a reading the proposal already has "
            f"need not be repeated, unless it changes the picture, such as a much higher median or fewer runs left out "
            f"for load. The owner approves a move or a higher budget ({SPLIT})"]


def lane_commands(config, name):
    lane = config.get("lanes", {}).get(name, {})
    if "parallel" in lane:
        return [command for member in lane["parallel"] for command in lane_commands(config, member)]
    return [lane["command"]] if "command" in lane else []


# The qk commands whose --device names a simulator device type, which the simulator pool supplies, and not a
# physical device: `qk simulator-tests --device iPhone-17-Pro` and `qk simulator-pool take|template --device TYPE`
# (vocaro/queuekeeper#113).
SIMULATOR_TYPES = ("simulator-tests", "simulator-pool")


def takes_device(command):
    """Whether a command reserves a physical device: a device pass (`qk device-pass`), `devices take` or `reserve`,
    or a --device option of any command but those whose --device names a simulator type (SIMULATOR_TYPES). Such an
    option belongs to the qk command before it up to a --, after which the words are another command's or arguments
    passed through, so a --device there counts."""
    if "device-pass" in command: return True
    if any(word == "devices" and command[index + 1:index + 2] in (["take"], ["reserve"])
           for index, word in enumerate(command)):
        return True
    in_qk = simulator = False
    for word in command:
        if word == "--" or Path(word).name == "qk":
            in_qk, simulator = Path(word).name == "qk", False
        elif in_qk and word in SIMULATOR_TYPES:
            simulator = True
        elif (word == "--device" or word.startswith("--device=")) and not simulator:
            return True
    return False


def passes_devices(command):
    """Whether a command is a device pass, `qk device-pass`, which takes the paired devices itself."""
    return "device-pass" in command


def misplaced_passes(config):
    """The lanes that run a device pass and would go to the second Mac, whose device record defers physical
    devices to this Mac, so the pass refuses there: an admitted lane, or group, without "here"."""
    lanes = config.get("lanes", {})
    found = []
    for name, lane in lanes.items():
        if "command" not in lane or not (passes_devices(lane["command"]) or lane.get("devices")): continue
        if lane.get("resource") != "self" and not lane.get("here"): found.append(name)
    return found


def doctor_warnings(config):
    """qk doctor's own: a direct-main gate with no budget, a gate that takes a physical device, and a device
    pass's lane that would go to the second Mac."""
    lanes = config.get("lanes", {})
    warnings = [f"{name} runs a device pass with no \"here\", so qk run sends it to the second Mac, whose device "
                "record defers physical devices to this Mac: give it \"here\": \"a physical device, reserved in "
                f"this Mac's device record\" ({PASS})" for name in misplaced_passes(config)]
    if GATE not in lanes: return warnings
    if config.get("integration", "direct-main") == "direct-main" and "budgetMinutes" not in lanes[GATE]:
        warnings.append(f"gate has no budgetMinutes, so nothing warns when it outgrows its time: set it in the gate "
                        f"lane from the gate's measured time ({SPLIT})")
    if any(takes_device(command) for command in lane_commands(config, GATE)):
        warnings.append(f"gate takes a physical device, which makes it a release check: move that check to a lane "
                        f"of its own named in release.lanes ({SPLIT})")
    return warnings


def landing_warnings(root, config, entries):
    """What a landing prints after its result, and doctor warns of too: the gate over its budget, and what the
    project's tooling adds (extensions.LANDING_WARNINGS). A check that cannot be made says so rather than stopping
    the landing."""
    warnings = []
    for check in (lambda: gate_warnings(config, entries), *(lambda added=added: added(root, config) for added in extensions.LANDING_WARNINGS)):
        try: warnings += check()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            warnings.append(f"could not check the gate's budget: {error}")
    return warnings
