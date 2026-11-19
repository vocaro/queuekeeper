"""Each lane's timing guidance: a baseline from its recent passing runs, a warning while a run goes past it, an optional
fail threshold relative to it, the notice an agent is shown when a limit stops a lane, and `qk timing`, which shows the
baselines and marks their history stale.

The budget (budgets.py) judges the gate after it lands, and stall detection (progress.py) a lane that goes quiet; a lane
that keeps printing but takes far longer than it used to said nothing until its timeout, which is a backstop for a
loaded host and not a measure of the lane. Here each passing whole run of a lane on this Mac appends its own work's
seconds, from its command's start to its end and without its wait for admission, to this Mac's timing history,
HISTORY beside the admission log (resources.log_path), by project and lane, with one previous generation as
wait_history keeps. A lane's baseline is the 90th percentile, by nearest rank, of its newest RECENT such runs since its
history was last marked stale, once it has FEWEST of them.

While a lane runs, past its baseline it says so once: a line in its log and on qk run's standard error, which the
queue's gate log keeps, and a record beside the lane's wait record (live_path) that `qk queue status`, `qk queue wait`
and their JSON read, which a run on another Mac sends back with its wait record. A lane may also give a fail threshold
relative to its baseline ("timing": {"fail": "+25%"} or "+2m"), which applies once the baseline has the lane's
minSamples runs (MIN_SAMPLES by default): past it the lane is stopped, as a stall is, and fails, as a lane that got
slower is a change's to explain. A run on a loaded host can go past it too, which is why it is opt-in.

A limit that stops a lane, its timeout, its stall bound or its fail threshold, shows the lane's notice (DEFAULT_NOTICE
unless the lane's "notice", or its stall's own, says otherwise), so that an agent fixes the slowness rather than
raises the limit.

`qk timing stale LANE --reason WHY` marks LANE's history, or every lane's with --all, stale as of now: a deliberate
change to a lane's workload then sets no warning or failure against runs before it, and the gate's budget median
(budgets.py) leaves out gate runs that settled before it. The marks travel with a dispatch to another Mac, whose own
history the lane's runs there are judged by (STALE_VARIABLE). Nothing here may stop or slow a lane: a history that
cannot be read or written is said on standard error and left as it is."""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import time

HISTORY, PREVIOUS, LOCK, LIMIT = "timings.jsonl", "timings.1.jsonl", "timings.lock", 2 << 20
# The newest passing runs a baseline is taken over, and the fewest that make one.
RECENT, FEWEST = 20, 5
# The runs a baseline needs before a fail threshold applies, unless the lane's minSamples says otherwise.
MIN_SAMPLES = 10
PERCENTILE = 90
# Every lane's history, in a stale mark.
EVERY_LANE = "*"
# The stale marks a dispatch carries to the Mac that runs it, as {LANE or "*": epoch time}.
STALE_VARIABLE = "QUEUEKEEPER_TIMING_STALE"
# What a limit that stops a lane tells its reader, unless the lane says otherwise.
DEFAULT_NOTICE = ("do not raise the limit just to make the gate pass: find what made the lane slower, or, when the "
                  "work grew on purpose, mark its timing history stale (qk timing stale LANE --reason WHY) and have "
                  "the owner approve a new limit")
# The line a timing warning or failure starts with, in a lane's log, the queue's gate log and qk run's standard error,
# which agent_results finds there.
PREFIX = "queuekeeper timing: "
NOTICE_PREFIX = "queuekeeper notice: "
LINE = re.compile(r"^" + re.escape(PREFIX) + r"(.+)$", re.M)
NOTICE_LINE = re.compile(r"^" + re.escape(NOTICE_PREFIX) + r"(.+)$", re.M)
THRESHOLD = re.compile(r"^\+(\d+(?:\.\d+)?)(%|s|m|h)$")
UNITS = {"s": 1, "m": 60, "h": 3600}
# The suffix of the live record beside a lane's wait record (lane_wait.VARIABLE).
LIVE = ".timing"


def path():
    """This Mac's timing history, beside its admission log, and so in a test's own HEAVY_SLOT_DIR namespace."""
    from queuekeeper import resources
    return resources.log_path().with_name(HISTORY)


def threshold(text):
    """A fail threshold's ("percent", 25.0) for "+25%", or ("seconds", 120.0) for "+2m"; ValueError for anything else."""
    matched = THRESHOLD.match(text) if isinstance(text, str) else None
    if not matched or float(matched.group(1)) <= 0:
        raise ValueError("must be a relative threshold such as \"+25%\", \"+90s\", \"+2m\" or \"+1h\"")
    amount, unit = float(matched.group(1)), matched.group(2)
    return ("percent", amount) if unit == "%" else ("seconds", amount * UNITS[unit])


def check(name, lane):
    """A lane's timing and notice settings, as config.parse checks them."""
    if "notice" in lane and (not isinstance(lane["notice"], str) or not lane["notice"].strip()):
        raise ValueError(f"{name}.notice must be the words shown when a limit stops the lane")
    if "timing" not in lane: return
    settings = lane["timing"]
    if not isinstance(settings, dict) or set(settings) - {"warn", "fail", "minSamples"}:
        raise ValueError(f"{name}.timing supports warn, fail and minSamples")
    if "warn" in settings and type(settings["warn"]) is not bool:
        raise ValueError(f"{name}.timing.warn must be true or false")
    if "fail" in settings:
        try: threshold(settings["fail"])
        except ValueError as error: raise ValueError(f"{name}.timing.fail {error}") from None
    if "minSamples" in settings and (type(settings["minSamples"]) is not int or settings["minSamples"] < FEWEST):
        raise ValueError(f"{name}.timing.minSamples must be an integer of at least {FEWEST}")


def notice(lane, stall=False):
    """The words a limit that stops lane shows: its stall's own for a stall, else the lane's, else DEFAULT_NOTICE."""
    if stall and isinstance((lane.get("stall") or {}).get("notice"), str): return lane["stall"]["notice"].strip()
    return lane["notice"].strip() if isinstance(lane.get("notice"), str) else DEFAULT_NOTICE


def notice_line(lane_name, lane, limit, stall=False):
    return f"{NOTICE_PREFIX}{lane_name} reached its {limit}: {notice(lane, stall)}"


def append(entry):
    """Append entry to this Mac's history under its lock, the previous generation taking the history past LIMIT."""
    try:
        log = path()
        log.parent.mkdir(parents=True, exist_ok=True)
        text = (json.dumps(entry) + "\n").encode()
        with (log.parent / LOCK).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.suppress(FileNotFoundError):
                size = log.stat().st_size
                if size and size + len(text) > LIMIT: log.replace(log.with_name(PREVIOUS))
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o644)
            try: os.write(fd, text)
            finally: os.close(fd)
        return True
    except Exception as error:  # noqa: BLE001 - a history that cannot be written never stops a lane
        with contextlib.suppress(Exception):
            print(f"queuekeeper: the timing history was not written: {error}", file=sys.stderr, flush=True)
        return False


def read():
    """The history's lines, oldest first, from both generations; a line that is no object is left out."""
    log = path()
    found = []
    for name in (log.with_name(PREVIOUS), log):
        try: lines = name.read_bytes().splitlines()
        except OSError: continue
        for text in lines:
            try: entry = json.loads(text)
            except ValueError: continue
            if isinstance(entry, dict) and number(entry.get("time")): found.append(entry)
    return found


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def record(project, lane, seconds, commit=None, at=None):
    """A passing whole run of lane took seconds of its own work."""
    if not number(seconds) or seconds <= 0: return False
    return append({"event": "run", "time": round(time.time() if at is None else at, 3), "project": project,
                   "lane": lane, "seconds": round(seconds, 1), **({"commit": commit} if commit else {})})


def mark_stale(project, lanes, reason, at=None):
    """Mark the history of each of lanes, or EVERY_LANE's, stale as of at (now)."""
    when = round(time.time() if at is None else at, 3)
    return all(append({"event": "stale", "time": when, "project": project, "lane": lane, "reason": reason})
               for lane in lanes)


def carried():
    """The stale marks a dispatch brought from the Mac that sent it (STALE_VARIABLE), as {LANE: epoch time}."""
    try: marks = json.loads(os.environ.get(STALE_VARIABLE) or "{}")
    except ValueError: return {}
    return {lane: when for lane, when in marks.items() if isinstance(lane, str) and number(when)} \
        if isinstance(marks, dict) else {}


def stale_marks(project, entries=None):
    """The newest stale mark of each lane of project, EVERY_LANE's among them, as {LANE: epoch time}, with those a
    dispatch carried here."""
    marks = carried()
    for entry in read() if entries is None else entries:
        if entry.get("event") != "stale" or entry.get("project") != project or not isinstance(entry.get("lane"), str):
            continue
        marks[entry["lane"]] = max(marks.get(entry["lane"], 0), entry["time"])
    return marks


def stale_since(marks, lane):
    """When lane's history was last marked stale, by its own mark or every lane's, or None."""
    found = [marks[key] for key in (lane, EVERY_LANE) if number(marks.get(key))]
    return max(found) if found else None


def percentile(values, share=PERCENTILE):
    """The nearest-rank percentile of values."""
    ordered = sorted(values)
    return ordered[max(math.ceil(share / 100 * len(ordered)) - 1, 0)]


def baseline(project, lane, entries=None):
    """Lane's baseline on this Mac: {"p90", "median", "samples", "staleSince"} over its newest RECENT passing runs
    since its last stale mark, or None with fewer than FEWEST of them."""
    entries = read() if entries is None else entries
    since = stale_since(stale_marks(project, entries), lane)
    runs = [entry["seconds"] for entry in entries
            if entry.get("event") == "run" and entry.get("project") == project and entry.get("lane") == lane
            and number(entry.get("seconds")) and (since is None or entry["time"] > since)][-RECENT:]
    if len(runs) < FEWEST: return None
    ordered = sorted(runs)
    return {"p90": percentile(runs), "median": ordered[len(ordered) // 2] if len(ordered) % 2
            else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2,
            "samples": len(runs), "staleSince": since}


def fail_seconds(settings, found):
    """The seconds past which a lane with these timing settings fails, against the baseline found, or None: no fail
    threshold, no baseline, or fewer runs in it than its minSamples."""
    if not isinstance(settings, dict) or "fail" not in settings or found is None: return None
    if found["samples"] < settings.get("minSamples", MIN_SAMPLES): return None
    kind, amount = threshold(settings["fail"])
    return found["p90"] * (1 + amount / 100) if kind == "percent" else found["p90"] + amount


def words(seconds):
    seconds = int(round(seconds))
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Pace:
    """A running lane's place against its baseline, which supervise asks every second or so (observe)."""

    def __init__(self, name, lane, project, entries=None, live=None, clock=time.monotonic):
        self.name, self.lane, self.clock = name, lane, clock
        settings = lane.get("timing") if isinstance(lane.get("timing"), dict) else {}
        self.warns = settings.get("warn", True)
        try: self.base = baseline(project, name, entries)
        except Exception:  # noqa: BLE001 - an unreadable history gives no baseline
            self.base = None
        try: self.limit = fail_seconds(settings, self.base)
        except ValueError: self.limit = None
        self.setting = settings.get("fail")
        self.live = live if live is not None else live_path()
        self.began, self.started, self.warned, self.failed = clock(), time.time(), None, None
        if self.live is not None:
            with contextlib.suppress(OSError): Path(self.live).unlink()

    def active(self):
        """Whether there is anything to watch: a baseline to warn against, or a threshold to fail past."""
        return self.base is not None and (self.warns or self.limit is not None)

    def summary(self):
        """What the receipt keeps of the run's timing."""
        if self.base is None: return None
        return {"p90Seconds": round(self.base["p90"], 1), "medianSeconds": round(self.base["median"], 1),
                "samples": self.base["samples"], **({"failSeconds": round(self.limit, 1)} if self.limit else {}),
                **({"warned": self.warned} if self.warned else {}), **({"failed": self.failed} if self.failed else {})}

    def observe(self):
        """(a line to log, or None; why the lane fails now, or None)."""
        if self.base is None: return None, None
        elapsed = self.clock() - self.began
        said = None
        if self.warns and self.warned is None and elapsed > self.base["p90"]:
            self.warned = (f"{self.name} has run {words(elapsed)}, past its p90 of {words(self.base['p90'])} over its "
                           f"last {self.base['samples']} passing runs on this Mac (median {words(self.base['median'])})"
                           + (f"; it fails past {words(self.limit)} ({self.setting})" if self.limit else ""))
            said = PREFIX + "warning: " + self.warned
            self.publish("slow", self.warned)
        if self.limit is not None and self.failed is None and elapsed > self.limit:
            self.failed = (f"{self.name} ran past {words(self.limit)}, its fail threshold {self.setting} over its p90 "
                           f"of {words(self.base['p90'])} over its last {self.base['samples']} passing runs on this Mac")
            self.publish("failed", self.failed)
            return PREFIX + "failed: " + self.failed, self.failed
        return said, None

    def publish(self, state, said):
        if self.live is None: return
        live = {"lane": self.name, "state": state, "words": said, "startedAt": self.started, "written": time.time(),
                "p90Seconds": round(self.base["p90"], 1), "samples": self.base["samples"],
                **({"failSeconds": round(self.limit, 1)} if self.limit else {})}
        write_live(self.live, live)


def live_path():
    """Where this run's live timing record goes: beside the lane's wait record, which a run on another Mac sends back
    (offload_host.relay_wait), or None for a run that names none."""
    from . import lane_wait
    wait = os.environ.get(lane_wait.VARIABLE)
    return Path(wait + LIVE) if wait else None


def write_live(where, record):
    where = Path(where)
    staged = where.with_name(f".{where.name}.{os.getpid()}")
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(record) + "\n")
        os.replace(staged, where)
    except OSError:
        with contextlib.suppress(OSError): staged.unlink()


def live(wait, since=None):
    """The live timing record beside the wait record at wait, or None: absent, unreadable, or written before since,
    the run's start."""
    try: found = json.loads(Path(str(wait) + LIVE).read_text())
    except (OSError, ValueError): return None
    if not isinstance(found, dict) or not isinstance(found.get("words"), str): return None
    if since is not None and (not number(found.get("written")) or found["written"] < since): return None
    return found


def relayed(record, wait):
    """A live timing record another Mac sent back with its wait record, written beside this Mac's record at wait."""
    if isinstance(record, dict) and isinstance(record.get("words"), str) and wait:
        write_live(Path(str(wait) + LIVE), {**record, "written": time.time()})


def found_in(text):
    """The timing lines and notices a log holds, in order, each once."""
    seen = {}
    for matched in (*LINE.finditer(text), *NOTICE_LINE.finditer(text)):
        seen.setdefault(matched.group(0).strip(), matched.start())
    return [line for line, _ in sorted(seen.items(), key=lambda item: item[1])]


def lanes_of(config):
    return sorted(name for name, lane in config.get("lanes", {}).items() if "command" in lane)


def show(project, config, as_json=False):
    """`qk timing show`: each lane's baseline on this Mac, its fail threshold, and when it was last marked stale."""
    entries = read()
    marks = stale_marks(project, entries)
    rows = []
    for name in lanes_of(config):
        lane = config["lanes"][name]
        found = baseline(project, name, entries)
        settings = lane.get("timing") if isinstance(lane.get("timing"), dict) else {}
        limit = fail_seconds(settings, found)
        rows.append({"lane": name, "baseline": found, "fail": settings.get("fail"),
                     "failSeconds": round(limit, 1) if limit else None, "warn": settings.get("warn", True),
                     "staleSince": stale_since(marks, name)})
    if as_json:
        print(json.dumps({"schemaVersion": 1, "project": project, "history": str(path()), "lanes": rows}, indent=2))
        return 0
    for row in rows:
        found = row["baseline"]
        since = " since it was marked stale" if row["staleSince"] else ""
        said = (f"p90 {words(found['p90'])}, median {words(found['median'])} over {found['samples']} passing runs"
                if found else f"no baseline yet (fewer than {FEWEST} passing runs{since})")
        if row["fail"]: said += f"; fails past {words(row['failSeconds'])} ({row['fail']})" if row["failSeconds"] \
            else f"; fail threshold {row['fail']} waits for more runs"
        if row["staleSince"]: said += f"; marked stale {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['staleSince']))}"
        print(f"{row['lane']}: {said}")
    print(f"history: {path()}")
    return 0


def main(root, argv):
    """`qk timing show|stale`."""
    from . import cli, config as configuration
    parser = argparse.ArgumentParser(prog="qk timing", description="Each lane's timing baseline on this Mac, and "
                                     "marking its history stale after a deliberate change to the lane's work")
    actions = parser.add_subparsers(dest="action", required=True)
    shown = actions.add_parser("show", help="each lane's baseline, fail threshold and stale mark")
    shown.add_argument("--json", action="store_true")
    stale = actions.add_parser("stale", help="mark lanes' timing history stale as of now")
    stale.add_argument("lanes", nargs="*", metavar="LANE")
    stale.add_argument("--all", action="store_true", help="every lane's")
    stale.add_argument("--reason", required=True, help="what changed the lanes' work on purpose")
    args = parser.parse_args(argv)
    config = configuration.load(root)
    project = cli.project_name(root, config)
    if args.action == "show": return show(project, config, args.json)
    if bool(args.lanes) == args.all: parser.error("name the lanes, or give --all")
    unknown = [lane for lane in args.lanes if lane not in config.get("lanes", {})]
    if unknown: parser.error(f"no such lane: {', '.join(unknown)}")
    if not args.reason.strip(): parser.error("--reason must say what changed")
    lanes = [EVERY_LANE] if args.all else args.lanes
    if not mark_stale(project, lanes, args.reason.strip()): return 1
    print(f"marked {'every lane' if args.all else ', '.join(args.lanes)} of {project} stale on this Mac: their "
          f"baselines and fail thresholds start again from their next {FEWEST} passing runs, and the gate's budget "
          f"median leaves out gate runs that settled before now; runs sent to another Mac carry the mark")
    return 0
