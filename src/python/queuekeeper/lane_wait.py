"""What a lane waits for now, in a small record beside its results, for `qk queue status` to read (#309).

`qk run` names the record, .build/queuekeeper/.waits/LANE.json, in VARIABLE for itself and everything it starts, and
removes it when it ends. A lane on another Mac has its own there, which offload_host sends back as it changes, as
`W` frames that the dispatcher writes to the lane's record here (relayed), so that the one file says it on any Mac.
The queue reads the record and no printed line: rewording what a wait prints changes nothing in status.

The record says what the lane waits for, since when and on which Mac, and whether the lane started there. The
lane's receipt (source.json) gives its verdict. A wait stops counting once the process that reported it is gone
(current).

As each wait of one kind ends on this Mac, its span is appended to this Mac's wait history (wait_history, ended), which
outlives the record: the record keeps when its kind began (kindSince) for it. A run's lane, project and whether it is
whole-host are named once, by the outermost run on each Mac (LANE), which also ends a wait the run leaves.

Nothing here may stop a lane: a record that cannot be written or read is left as it is."""
import contextlib
import json
import os
from pathlib import Path
import time
import uuid

# Set by the outermost `qk run` for itself and all it starts, and by offload_host for the run it starts.
VARIABLE = "QUEUEKEEPER_LANE_WAIT"
# Set by the outermost `qk run` on each Mac: the lane whose run it is, its project and whether it is whole-host, which
# each line of the wait history names (wait_history).
LANE = "QUEUEKEEPER_LANE_WAIT_LANE"
WAITS = ".waits"
HERE = "this Mac"
SCHEMA = 1

ADMISSION, SCREEN, FOREGROUND, SPACE = "admission", "screen", "foreground", "space"
# Why a lane waits for this Mac instead of another Mac; said beside the admission wait that follows it.
UNREACHABLE, ALONE, OVERFLOW = "unreachable", "alone", "overflow"
OFFERS = (UNREACHABLE, ALONE, OVERFLOW)
# What `qk queue status` says after "waiting: ". Each wait's words live here and nowhere in the queue.
WORDS = {ADMISSION: "admission on {mac}: {reason}",
         SCREEN: "an unlocked screen: {reason}",
         SPACE: "free disk space: {reason}",
         FOREGROUND: "another Mac's foreground, {reason}",
         UNREACHABLE: "another Mac cannot be reached{probes}, and is asked again within a minute: {reason}",
         ALONE: "this Mac alone, as another Mac cannot take it: {reason}",
         OVERFLOW: "whichever Mac admits it first, the farm's other Macs asked again{probes}: {reason}"}
# What identifies a state of the record, apart from when it was written.
STATE = ("kind", "reason", "mac", "offer", "stranded", "pid", "admitted", "probes")

_owned = None  # the record this process named, and so removes (begin, finish)
_offer = None  # why this run waits for this Mac instead of another Mac, until it is admitted
_closes = None  # whether this process named the run's lane (LANE), and so ends a wait the run leaves (finish)


def relative(lane):
    """Where a checkout keeps LANE's record: apart from .build/queuekeeper/LANE, which a run here alone creates."""
    return f".build/queuekeeper/{WAITS}/{lane}.json"


def path_of(root, lane):
    return Path(root) / relative(lane)


def begin(root, lane, project=None, whole_host=False):
    """Name LANE's record for this run and what it starts, with nothing an earlier run left in it. A run nested in
    another lane, or sent here by a dispatch, reports into the record already named: that lane's wait is this one.
    The outermost run on this Mac, one sent here included, names its lane, project and whether it is whole-host for
    the wait history (LANE)."""
    global _owned, _offer, _closes
    if not os.environ.get(LANE):
        os.environ[LANE] = json.dumps({"lane": lane, "project": project, "wholeHost": bool(whole_host)})
        _closes = True
    if os.environ.get(VARIABLE): return
    _owned, _offer = path_of(root, lane), None
    remove(_owned)
    os.environ[VARIABLE] = str(_owned)
    os.environ["QUEUEKEEPER_LANE_PROGRESS_RUN"] = uuid.uuid4().hex


def finish():
    """The run named by begin has ended, however it ended: no wait of it remains. A wait it leaves, as one that
    outlasts its limit does, ends now in the wait history."""
    global _owned, _closes
    if _closes:
        if os.environ.get(VARIABLE): ended(read(os.environ[VARIABLE]), "the run ended")
        os.environ.pop(LANE, None)
        _closes = None
    if _owned is None: return
    remove(_owned)
    os.environ.pop(VARIABLE, None)
    os.environ.pop("QUEUEKEEPER_LANE_PROGRESS_RUN", None)
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
                                       **({"progressRun": os.environ["QUEUEKEEPER_LANE_PROGRESS_RUN"]}
                                          if os.environ.get("QUEUEKEEPER_LANE_PROGRESS_RUN") else {})}) + "\n")
        os.replace(staged, path)
    except OSError: remove(staged)


def ended(found, how, at=None):
    """Append the wait found, a record, to this Mac's wait history as ended at at (now), how saying how: none for a
    record that waits for nothing, or that waits on another Mac, whose own history has it."""
    if not found or found.get("kind") not in WORDS or found.get("mac") != HERE: return
    start = found.get("kindSince", found.get("since"))
    if not isinstance(start, (int, float)): return
    try: named = json.loads(os.environ.get(LANE) or "{}")
    except ValueError: named = {}
    if not isinstance(named, dict): named = {}
    from . import wait_history
    wait_history.append(wait_history.line(
        project=named.get("project") if isinstance(named.get("project"), str) else None,
        lane=named.get("lane") if isinstance(named.get("lane"), str) else Path(os.environ.get(VARIABLE, "lane")).stem,
        kind=found["kind"], start=start, end=time.time() if at is None else at,
        whole_host=named.get("wholeHost") if isinstance(named.get("wholeHost"), bool) else None,
        reason=found.get("reason"), ended=how, dispatched_from=os.environ.get("QUEUEKEEPER_DISPATCHED_FROM")))


def same(found, record):
    return (found is not None and all(found.get(key) == record[key] for key in STATE)
            and found.get("progressRun") == os.environ.get("QUEUEKEEPER_LANE_PROGRESS_RUN"))


def waiting(kind, reason, mac=HERE, stranded=False, probes=None):
    """The lane now waits for this, for a reason in plain words, on mac; since when carries over a wait that goes on
    under other words, so that its start is the wait's and not the latest wording's. A wait on this Mac says beside
    it why the lane is not on another Mac (OFFERS). stranded says that dead or uncertain guard entries hold it.
    probes, for UNREACHABLE, is how many offers in a row could not reach another Mac and when the last failed
    (remote.Offer.probes), whose age words() gives as it reads the record (vocaro/queuekeeper#25)."""
    global _offer
    if not os.environ.get(VARIABLE): return
    if mac == HERE and kind in OFFERS: _offer = {"kind": kind, "reason": reason, "probes": probes}
    record = {"kind": kind, "reason": reason, "mac": mac, "offer": _offer if mac == HERE and kind not in OFFERS else None,
              "stranded": stranded, "pid": os.getpid(), "admitted": False, "probes": probes}
    found = read(os.environ[VARIABLE])
    if same(found, record): return
    now = time.time()
    since = found.get("since") if found and found.get("kind") else None
    # A wait of the same kind on the same Mac goes on under other words; any other ends the one before it.
    going = found is not None and found.get("kind") == kind and found.get("mac") == mac
    kind_since = found.get("kindSince", found.get("since")) if going else None
    if found and found.get("kind") and not going: ended(found, f"became a wait for {kind}", now)
    write({**record, "since": since if isinstance(since, (int, float)) else now,
           "kindSince": kind_since if isinstance(kind_since, (int, float)) else now})


def running(mac=HERE, *, admitted=True):
    """Clear the wait. Only a confirmed start on mac establishes placement for a stacked gate."""
    global _offer
    if not os.environ.get(VARIABLE): return
    _offer = None
    record = {"kind": None, "reason": None, "mac": mac, "offer": None, "stranded": False,
              "pid": os.getpid(), "admitted": admitted, "probes": None}
    found = read(os.environ[VARIABLE])
    if same(found, record): return
    ended(found, "cleared")
    write({**record, "since": None})


def farm_mac(host):
    """What a record calls the farm's Mac at the SSH destination host: its name in the farm (farm.named)."""
    from . import farm
    return farm.named(host)


def host_of(mac):
    """The SSH destination of the farm's Mac that farm_mac named in mac, or None for this Mac or for words it
    did not write. A record written before a Mac had a name in the farm says `another Mac (HOST)`."""
    if not isinstance(mac, str) or mac == HERE: return None
    if mac.startswith("the second Mac (") and mac.endswith(")") and len(mac) > 17:
        return mac[len("the second Mac ("):-1]
    from . import farm
    with contextlib.suppress(ValueError):
        for found in farm.read():
            if found["name"] == mac: return found["ssh"]
    # A Mac reached by an SSH destination the farm does not list is named by that destination, a single word.
    return mac if mac and not mac.startswith("this Mac") and not any(map(str.isspace, mac)) else None


def sent(host):
    """The lane has gone to host to run. A wait goes on until another Mac takes it (relayed) or declines it."""
    if not os.environ.get(VARIABLE): return
    found = read(os.environ[VARIABLE])
    if not (found and found.get("kind")): running(farm_mac(host), admitted=False)


def taken_there():
    """Whether another Mac has started this run's lane, as its record says once that Mac's admission is relayed
    (relayed): True or False, or None when no record says, as for a run that names none (`qk admit`) or one whose
    record cannot be read. A place in this Mac's queue is kept only while this is False (remote.overflow)."""
    if not os.environ.get(VARIABLE): return None
    found = read(os.environ[VARIABLE])
    if found is None: return None
    return found.get("kind") is None and found.get("admitted") is True and host_of(found.get("mac")) is not None


def relayed(data, host):
    """What the lane's record on host now says, as its `W` frame carries it (offload_host.relay_wait): a wait, or
    nothing to wait for, in this Mac's record of the lane, named for the Mac it is on and by this Mac's clock."""
    try: record = json.loads(data)
    except ValueError: return
    if isinstance(record, dict) and "timing" in record:
        from . import lane_timing
        lane_timing.relayed(record["timing"], os.environ.get(VARIABLE))
    if isinstance(record, dict) and ("progress" in record or "timing" in record):
        if "progress" in record:
            from . import lane_progress
            lane_progress.relayed(record["progress"], host)
        if "kind" not in record: return
    if isinstance(record, dict) and record.get("kind") in WORDS:
        waiting(record["kind"], record.get("reason"), farm_mac(host), bool(record.get("stranded")))
    else: running(farm_mac(host), admitted=isinstance(record, dict) and record.get("admitted") is True)


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
    """qk doctor's pointer for each lane of this checkout waiting for admission now: to `qk admissions now`, which
    shows who holds and who waits on each Mac (vocaro-engineering#342). Doctor neither runs that report nor fails on it."""
    found = []
    try: paths = sorted((Path(root) / ".build/queuekeeper" / WAITS).glob("*.json"))
    except OSError: return []
    for path in paths:
        record = current(path)
        if not record or record.get("kind") != ADMISSION: continue
        since = record.get("since")
        waited = f" {int((at or time.time()) - since)}s" if isinstance(since, (int, float)) else ""
        found.append(f"lane {path.stem} has waited{waited} for {words(record)}; `python3 scripts/qk admissions now` "
                     "shows who holds and who waits for admission on each Mac")
    return found


def words(record, at=None):
    """What the lane waits for, in the words `qk queue status` shows, or None when it waits for nothing. Another Mac
    that cannot be reached is said with how many offers in a row failed to reach it and how long ago, at at (an epoch
    time; now), the last did (probed)."""
    if not record or not record.get("kind"): return None
    said = WORDS[record["kind"]].format(mac=record.get("mac") or HERE, reason=record.get("reason"),
                                        probes=probed(record.get("probes"), at))
    offer = record.get("offer")
    if isinstance(offer, dict) and offer.get("kind") in WORDS:
        said += "; " + WORDS[offer["kind"]].format(mac=HERE, reason=offer.get("reason"),
                                                   probes=probed(offer.get("probes"), at))
    return said


def probed(probes, at=None):
    """The verdict's count and age, as " (2 probes failed, the last 40 s ago)", or nothing when probes says neither;
    with the Mac's name first when probes names one of several (mac), as " (NAME: 1 probe failed, the last 9 s ago)"."""
    if not isinstance(probes, dict): return ""
    failed, last, mac = probes.get("failed"), probes.get("last"), probes.get("mac")
    if not isinstance(failed, int) or not isinstance(last, (int, float)): return ""
    age = max(int((time.time() if at is None else at) - last), 0)
    named = f"{mac}: " if isinstance(mac, str) and mac else ""
    return f" ({named}{failed} probe{'' if failed == 1 else 's'} failed, the last {age} s ago)"
