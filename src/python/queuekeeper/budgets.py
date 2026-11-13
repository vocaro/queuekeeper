"""The warnings about the landing gate's time budget (the toolkit's guides/releasing.md, Keeping the split,
vocaro-engineering#135), which a landing prints after its result and `doctor` gives:

- the gate's median wall time over its recent passing runs in the merge queue made on an unloaded host exceeds the
  gate lane's budgetMinutes, naming its slowest parts and how many loaded runs it left out;
- and, from doctor alone, a direct-main gate with no budget, a gate that takes a physical device, and a
  device pass's lane that would go to the second Mac.

A project's own tooling adds what else a landing warns of, such as a release gate's commit limit
(extensions.LANDING_WARNINGS).

The queue's worker records each landed entry's gate run (Worker.gate): its wall time as the queue saw
it, wherever the lane ran, any wait for admission included, the parts its log reports with their
times, and the CPU busy share of the Mac it ran on over the run, beside the busy limit that Mac sets (cpuBusy and
busyLimit, merge_queue.BusySampler). The budget and the owner's under-20-minute goal assume an unloaded machine, so
a run whose busy share was at or above its limit is left out of the median (owner's decision of 2026-11-07,
vocaro-engineering#235); a run recorded without a reading counts.
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


def loaded(run):
    """Whether a gate run was made on a loaded host: its CPU busy share over the run at or above the busy limit of the
    Mac it ran on, the limit at which admission finds no spare capacity. A run without both readings is not."""
    return number(run.get("cpuBusy")) and number(run.get("busyLimit")) and run["cpuBusy"] >= run["busyLimit"]


def busy_words(busy):
    """The worker log's words for a gate run's busy share (Worker.gate), saying when the budget leaves it out."""
    if not number(busy.get("cpuBusy")) or not number(busy.get("busyLimit")):
        return "the CPU busy share of the Mac it ran on could not be read"
    return (f"the CPU of {busy.get('busyOn', 'the Mac it ran on')} was {busy['cpuBusy']:g}% busy on average over "
            f"{busy.get('busyReadings', 1)} reading(s), at most {busy.get('cpuBusyHighest', busy['cpuBusy']):g}%, against "
            f"its busy limit of {busy['busyLimit']:g}%"
            + (": a loaded run, which the gate's budget median leaves out" if loaded(busy) else ""))


def gate_warnings(config, entries):
    """A warning when the median of the gate's RECENT_RUNS latest passing runs made on an unloaded host (loaded)
    exceeds its budget, saying how many loaded runs newer than the oldest of them it left out. None without a
    budget, or with fewer than FEWEST_RUNS such runs to judge it by."""
    budget = config.get("lanes", {}).get(GATE, {}).get("budgetMinutes")
    if budget is None: return []
    recent, left_out = [], 0
    for run in runs(entries):
        if len(recent) == RECENT_RUNS: break
        if loaded(run): left_out += 1
        else: recent.append(run)
    if len(recent) < FEWEST_RUNS: return []
    median = statistics.median(run["seconds"] for run in recent)
    if median <= budget * 60: return []
    slowest = next((run["parts"] for run in recent if run.get("parts")), [])[:NAMED_PARTS]
    named = (f"; its slowest parts in its latest run that reports them: "
             + ", ".join(f"{name} {seconds:g}s" for name, seconds in slowest)) if slowest \
        else "; its log reports no parts with their times"
    loads = (f", leaving out {left_out} run{'' if left_out == 1 else 's'} made with the CPU of the Mac it ran on at or "
             f"above its busy limit, as the budget assumes an unloaded host") if left_out else ""
    return [f"gate's median wall time over its last {len(recent)} passing runs in the merge queue is "
            f"{minutes(median)}{loads}, over its budget of {budget} min (gate.budgetMinutes){named}. Propose on the "
            f"project's issue tracker which check to move to release.lanes; the owner approves a move or a "
            f"higher budget ({SPLIT})"]


def lane_commands(config, name):
    lane = config.get("lanes", {}).get(name, {})
    if "parallel" in lane:
        return [command for member in lane["parallel"] for command in lane_commands(config, member)]
    return [lane["command"]] if "command" in lane else []


def takes_device(command):
    """Whether a command reserves a physical device: a --device option, `devices take` or `reserve`, or a device
    pass (`qk device-pass`)."""
    if any(word == "--device" or word.startswith("--device=") or word == "device-pass" for word in command):
        return True
    return any(word == "devices" and command[index + 1:index + 2] in (["take"], ["reserve"])
               for index, word in enumerate(command))


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
