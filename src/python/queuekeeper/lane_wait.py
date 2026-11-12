"""What a lane waits for now, in a small record beside its results, for `eng queue status` to read (#309).

`eng run` names the record, .build/engineering/.waits/LANE.json, in VARIABLE for itself and everything it starts, and
removes it when it ends. A lane on the second Mac has its own there, which offload_host sends back as it changes, as
`W` frames that the dispatcher writes to the lane's record here (relayed), so that the one file says it on either Mac.
The queue reads the record and no printed line: rewording what a wait prints changes nothing in status.

The record says what the lane waits for, since when and on which Mac, and whether the lane started there. The
lane's receipt (source.json) gives its verdict. A wait stops counting once the process that reported it is gone
(current).

Nothing here may stop a lane: a record that cannot be written or read is left as it is."""
import json
import os
from pathlib import Path
import time
import uuid

# Set by the outermost `eng run` for itself and all it starts, and by offload_host for the run it starts.
VARIABLE = "VOCARO_LANE_WAIT"
WAITS = ".waits"
HERE = "this Mac"
SCHEMA = 1

ADMISSION, SCREEN, FOREGROUND = "admission", "screen", "foreground"
# Why a lane waits for this Mac instead of the second Mac; said beside the admission wait that follows it.
UNREACHABLE, ALONE, OVERFLOW = "unreachable", "alone", "overflow"
OFFERS = (UNREACHABLE, ALONE, OVERFLOW)
# What `eng queue status` says after "waiting: ". Each wait's words live here and nowhere in the queue.
WORDS = {ADMISSION: "admission on {mac}: {reason}",
         SCREEN: "an unlocked screen: {reason}",
         FOREGROUND: "the second Mac's foreground, {reason}",
         UNREACHABLE: "the second Mac cannot be reached, and is asked again at the next offer: {reason}",
         ALONE: "this Mac alone, as the second Mac cannot take it: {reason}",
         OVERFLOW: "whichever Mac admits it first, the second Mac asked again: {reason}"}
# What identifies a state of the record, apart from when it was written.
STATE = ("kind", "reason", "mac", "offer", "stranded", "pid", "admitted")

_owned = None  # the record this process named, and so removes (begin, finish)
_offer = None  # why this run waits for this Mac instead of the second Mac, until it is admitted


def relative(lane):
    """Where a checkout keeps LANE's record: apart from .build/engineering/LANE, which a run here alone creates."""
    return f".build/engineering/{WAITS}/{lane}.json"


def path_of(root, lane):
    return Path(root) / relative(lane)


def begin(root, lane):
    """Name LANE's record for this run and what it starts, with nothing an earlier run left in it. A run nested in
    another lane, or sent here by a dispatch, reports into the record already named: that lane's wait is this one."""
    global _owned, _offer
    if os.environ.get(VARIABLE): return
    _owned, _offer = path_of(root, lane), None
    remove(_owned)
    os.environ[VARIABLE] = str(_owned)
    os.environ["VOCARO_LANE_PROGRESS_RUN"] = uuid.uuid4().hex


def finish():
    """The run named by begin has ended, however it ended: no wait of it remains."""
    global _owned
    if _owned is None: return
    remove(_owned)
    os.environ.pop(VARIABLE, None)
    os.environ.pop("VOCARO_LANE_PROGRESS_RUN", None)
    _owned = None


def remove(path):
    try: Path(path).unlink()
    except OSError: pass


def read(path):
    try: found = json.loads(Path(path).read_text())
    except (OSError, ValueError): return None
    return found if isinstance(found, dict) and found.get("schemaVersion") == SCHEMA else None


def write(record):
    path = Path(os.environ[VARIABLE])
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The folder ignores itself: a record is no change to the tree a lane's receipt identifies, in a checkout whose
        # own .gitignore leaves .build out too (a run's identity would otherwise not be its commit's).
        ignored = path.parent / ".gitignore"
        if not ignored.exists(): ignored.write_text("*\n")
        staged.write_text(json.dumps({"schemaVersion": SCHEMA, **record, "written": time.time(),
                                       **({"progressRun": os.environ["VOCARO_LANE_PROGRESS_RUN"]}
                                          if os.environ.get("VOCARO_LANE_PROGRESS_RUN") else {})}) + "\n")
        os.replace(staged, path)
    except OSError: remove(staged)


def same(found, record):
    return (found is not None and all(found.get(key) == record[key] for key in STATE)
            and found.get("progressRun") == os.environ.get("VOCARO_LANE_PROGRESS_RUN"))


def waiting(kind, reason, mac=HERE, stranded=False):
    """The lane now waits for this, for a reason in plain words, on mac; since when carries over a wait that goes on
    under other words, so that its start is the wait's and not the latest wording's. A wait on this Mac says beside
    it why the lane is not on the second Mac (OFFERS). stranded says that dead or uncertain guard entries hold it."""
    global _offer
    if not os.environ.get(VARIABLE): return
    if mac == HERE and kind in OFFERS: _offer = {"kind": kind, "reason": reason}
    record = {"kind": kind, "reason": reason, "mac": mac, "offer": _offer if mac == HERE and kind not in OFFERS else None,
              "stranded": stranded, "pid": os.getpid(), "admitted": False}
    found = read(os.environ[VARIABLE])
    if same(found, record): return
    since = found.get("since") if found and found.get("kind") else None
    write({**record, "since": since if isinstance(since, (int, float)) else time.time()})


def running(mac=HERE, *, admitted=True):
    """Clear the wait. Only a confirmed start on mac establishes placement for a stacked gate."""
    global _offer
    if not os.environ.get(VARIABLE): return
    _offer = None
    record = {"kind": None, "reason": None, "mac": mac, "offer": None, "stranded": False,
              "pid": os.getpid(), "admitted": admitted}
    if not same(read(os.environ[VARIABLE]), record): write({**record, "since": None})


def second_mac(host):
    return f"the second Mac ({host})"


def sent(host):
    """The lane has gone to host to run. A wait goes on until the second Mac takes it (relayed) or declines it."""
    if not os.environ.get(VARIABLE): return
    found = read(os.environ[VARIABLE])
    if not (found and found.get("kind")): running(second_mac(host), admitted=False)


def relayed(data, host):
    """What the lane's record on host now says, as its `W` frame carries it (offload_host.relay_wait): a wait, or
    nothing to wait for, in this Mac's record of the lane, named for the Mac it is on and by this Mac's clock."""
    try: record = json.loads(data)
    except ValueError: return
    if isinstance(record, dict) and "progress" in record:
        from . import lane_progress
        lane_progress.relayed(record["progress"], host)
        if "kind" not in record: return
    if isinstance(record, dict) and record.get("kind") in WORDS:
        waiting(record["kind"], record.get("reason"), second_mac(host), bool(record.get("stranded")))
    else: running(second_mac(host), admitted=isinstance(record, dict) and record.get("admitted") is True)


def alive(pid):
    try: os.kill(pid, 0)
    except (OSError, OverflowError, TypeError): return False
    return True


def current(path, since=None):
    """The record's state, or None when it says nothing true now: absent, unreadable, written before since (an epoch
    time, the run's start), or a wait whose process is gone, which can no longer be waiting."""
    record = read(path)
    if record is None or record.get("kind") not in (None, *WORDS): return None
    written = record.get("written")
    if since is not None and (not isinstance(written, (int, float)) or written < since): return None
    if record.get("kind") and not alive(record.get("pid")): return None
    return record


def admission_warnings(root, at=None):
    """eng doctor's pointer for each lane of this checkout waiting for admission now: to `eng admissions now`, which
    shows who holds and who waits on each Mac (vocaro-engineering#342). Doctor neither runs that report nor fails on it."""
    found = []
    try: paths = sorted((Path(root) / ".build/engineering" / WAITS).glob("*.json"))
    except OSError: return []
    for path in paths:
        record = current(path)
        if not record or record.get("kind") != ADMISSION: continue
        since = record.get("since")
        waited = f" {int((at or time.time()) - since)}s" if isinstance(since, (int, float)) else ""
        found.append(f"lane {path.stem} has waited{waited} for {words(record)}; `python3 scripts/qk admissions now` "
                     "shows who holds and who waits for admission on each Mac")
    return found


def words(record):
    """What the lane waits for, in the words `eng queue status` shows, or None when it waits for nothing."""
    if not record or not record.get("kind"): return None
    said = WORDS[record["kind"]].format(mac=record.get("mac") or HERE, reason=record.get("reason"))
    offer = record.get("offer")
    if isinstance(offer, dict) and offer.get("kind") in WORDS:
        said += "; " + WORDS[offer["kind"]].format(mac=HERE, reason=offer.get("reason"))
    return said
