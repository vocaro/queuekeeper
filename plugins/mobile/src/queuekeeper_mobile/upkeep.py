"""What Queuekeeper puts right in this Mac's device record by itself, with no session asking: a run whose process is
proven gone, and an owner's notifications left silenced past a held device's window (the owner's decisions,
2026-11-19: "i want to avoid recovery by hand and make it automatic wherever possible", vocaro/queuekeeper#230,
and option (b) with a direct notice, vocaro/queuekeeper#174).

**A run proven gone** (recover_run). An entry whose run is active while its recorded process has ended, or whose
PID now names another process (a start other than the recorded one, or after the run began), is an orphan nobody
need recover by hand. Queuekeeper undoes what the run's record says it changed: it stops each process the run
recorded as its command (`run.processes`) that still runs with its recorded start, puts the owner's notifications
back when a restore is pending, and ends the run, recording each step in the entry's `runRecoveries`. The
reservation stays with its holder. It falls back to attended recovery (`devices recover`) only when the end cannot be
proven, the entry is uncertain, the run went to another Mac, or a step fails; then it records the step, says it and
names the command that finishes it, and does not try that run again by itself. A notification shortcut that fails
leaves the restore pending, which the holder's release or the overdue rule runs again, and does not hold the
recovery up.

**An overdue silencing** (restore_overdue). A held physical device whose expected release passed GRACE seconds ago
while its entry still records `restore: pending` may be keeping Do Not Disturb on the owner's devices after the
time he agreed. Queuekeeper runs the platform's restore (on an iPhone, Vocaro Restore Notifications), records it in
the entry's notifications, with `silence: put back`, so that a later run on it says it is not silenced, and tells the
owner with a banner on this Mac (queuekeeper_mac_desktop.owner_notice). The reservation stays held. A restore that
fails stays pending and is tried again after RETRY seconds; the owner is told of the first failure.

**Where it runs.** Nothing here runs on a timer of its own. Every command that reaches the hosts (`qk run`,
`admit`, `remote`, `land`, `queue`, `disk`) looks, cheaply, and starts upkeep apart from itself when something is due
(spawn, plugins.HOST_COMMAND); the queue's signed launcher runs such a command every two minutes, and agents run them
all day, as the notify plugin's stuck-queue check relies on. `devices status`, `qk doctor`, `devices take` while it
waits, `devices run`, `devices release` and the device pass run it themselves first, so the next pass on a device
never refuses an orphan its run left. One upkeep runs at a time (LOCK); the others wait for it, briefly, or skip.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import os
import signal
import subprocess
import sys
import time

from queuekeeper import offload_host, processes
from queuekeeper.config import TOOLKIT_ROOT
from queuekeeper.resources import process_start
from . import devices
from .devices import DEFERRED, Refused, Stale, condition, moment, now, physical, running

# Seconds past a held device's expected release before Queuekeeper puts the owner's notifications back by itself.
GRACE = 15 * 60
# Seconds instead of GRACE, for a check on a real device that cannot wait fifteen minutes.
GRACE_VARIABLE = "QUEUEKEEPER_DEVICE_RESTORE_GRACE"
# Seconds before a restore that failed is tried again.
RETRY = 10 * 60
# "off": no command starts upkeep by itself (spawn); tests set it for every process they start (host_isolation).
SWITCH = "QUEUEKEEPER_DEVICE_UPKEEP"
# Seconds between two upkeeps a command starts by itself.
EVERY = 60
# Seconds an upkeep a command runs itself waits for one already running before it goes on without.
WAIT = 200
# Seconds a stopped process has to end after SIGTERM, then after SIGKILL.
TERM_WAIT, KILL_WAIT = 10, 5
# Beside the record: one upkeep at a time, and when a command last started one.
LOCK, STAMP = "device-upkeep.lock", "device-upkeep.stamp"
# What an entry keeps of its automatic recoveries, the last few, and of one that fell back to its owner.
RECOVERIES, FALLBACK, KEPT = "runRecoveries", "runRecoveryFailed", 10
# What a notification's silence says once Queuekeeper put the notifications back while the device stayed held.
PUT_BACK = "put back"


def grace():
    """GRACE, or the seconds GRACE_VARIABLE names."""
    try: return max(0.0, float(os.environ[GRACE_VARIABLE]))
    except (KeyError, ValueError): return GRACE


def _run_of(entry):
    return entry.get("run") if isinstance(entry.get("run"), dict) else {}


def identity(entry):
    """What tells this run from the next one on the same entry."""
    return (entry.get("reservedAt"), entry.get("ownerChatID"), entry.get("hostProcessIdentifier"),
            entry.get("hostProcessStart"), _run_of(entry).get("startedAt"))


def proof(entry):
    """Why entry's active run is proven gone, or None when it is not: its recorded process has ended, or its PID now
    names another process, one with another start than the recorded one or, with none recorded, one that started
    after the run began."""
    pid, start = entry.get("hostProcessIdentifier"), entry.get("hostProcessStart")
    if type(pid) is not int or pid <= 0: return None
    try: found = process_start(pid)
    except OSError: return None
    if found is None: return f"its process {pid} has ended"
    if start is not None:
        return None if found == start else \
            f"process {pid} is another process now, started {found}, not the run's, started {start}"
    began, born = moment(_run_of(entry).get("startedAt")), processes.birth_time(found)
    if began is not None and born is not None and born > began.timestamp() + 1:
        return f"process {pid} is another process now, started {found}, after the run began at {began.isoformat()}"
    return None


def unproven(key, entry):
    """Why an orphaned entry's run is left for attended recovery, or None when Queuekeeper may recover it."""
    if condition(entry) != "orphan": return f"{key} is {condition(entry)}, not orphaned"
    if _run_of(entry).get("host"):
        return f"its run went to {_run_of(entry)['host']}, which only an attended recovery checks"
    if proof(entry) is None:
        return (f"its run's process {entry.get('hostProcessIdentifier')} is not proven gone: it is unrecorded, or "
                "still runs with no start recorded to tell it from the run's")
    failed = entry.get(FALLBACK)
    if isinstance(failed, dict) and tuple(failed.get("identity") or ()) == identity(entry):
        return f"its automatic recovery stopped at {failed.get('step')}: {failed.get('detail')}"
    return None


def overdue(entry, at=None):
    """Whether entry is a held physical device GRACE past its expected release with a restore pending, whose last
    automatic restore, if any, failed RETRY seconds ago or more."""
    at = at or datetime.now(timezone.utc)
    if not physical(entry.get("device")) or condition(entry) in ("released", "active"): return False
    notifications = entry.get("notifications") or {}
    if notifications.get("restore") != "pending": return False
    deadline = moment(entry.get("expectedReleaseBy"))
    if deadline is None or deadline + timedelta(seconds=grace()) > at: return False
    tried = moment(notifications.get("putBackTriedAt"))
    return tried is None or tried + timedelta(seconds=RETRY) <= at


def due(value, at=None):
    """The keys of value's entries Queuekeeper would put right now, by itself, each with what it would do."""
    if value.get(DEFERRED): return []
    found = []
    for key, entry in value["devices"].items():
        if not isinstance(entry, dict) or not physical(entry.get("device")): continue
        if condition(entry) == "orphan" and unproven(key, entry) is None: found.append((key, "run"))
        elif overdue(entry, at): found.append((key, "overdue"))
    return found


def _settle(record, key, before, change):
    """Write change into key's entry while it is still the run or holding before was, reading it again when its
    revision moved on meanwhile; the entry, or None when it changed into another."""
    revision = before.get("revision", 0)
    for _attempt in range(5):
        try:
            return record._change(key, revision, change)
        except Stale:
            current = record.entry(key) or {}
            if identity(current) != identity(before): return None
            revision = current.get("revision", 0)
    return None


def _fell_back(record, key, entry, step, detail, said):
    """Record and say that the automatic recovery of key's run stopped at step, naming what finishes it."""
    owner = entry.get("ownerChatID")
    command = offload_host.recover_command(key, "N", toolkit=TOOLKIT_ROOT)
    said(f"devices: {key}'s run process is gone, but Queuekeeper could not recover it by itself: {step} failed: "
         f"{detail}. Once that is settled, its holder {owner} or the owner finishes it with `{command}`")

    def change(current, _record):
        return {**current, FALLBACK: {"at": now(), "step": step, "detail": detail, "identity": list(identity(entry)),
                                      "finish": command}}
    _settle(record, key, entry, change)
    return None


def _stop(item):
    """End the process item names, {"pid", "start"}, and its process group, while it is still that process; why
    it still runs, or None once it has ended."""
    pid = item["pid"]

    def same():
        return process_start(pid) == item["start"]
    for number, wait in ((signal.SIGTERM, TERM_WAIT), (signal.SIGKILL, KILL_WAIT)):
        if not same(): return None
        for send in (os.killpg, os.kill):
            try: send(pid, number)
            except (ProcessLookupError, PermissionError): pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not same(): return None
            time.sleep(0.1)
    return None if not same() else f"process {pid} still runs after SIGKILL"


def recover_run(record, key, said):
    """Recover key's orphaned run by itself when its end is proven (unproven): stop the processes the run recorded,
    put the owner's notifications back when a restore is pending, and end the run, keeping the reservation with its
    holder. The entry it recorded, or None when it left it for attended recovery or another change came first."""
    entry = record.entry(key) or {}
    why = unproven(key, entry)
    if why is not None: return None
    shown = proof(entry)
    steps = []
    run = _run_of(entry)
    alive = running(entry)
    if alive and "processes" not in run:
        return _fell_back(record, key, entry, "stopping its processes",
                          f"processes {', '.join(str(item['pid']) for item in alive)} that the entry records still run, "
                          "and its run does not say which of them are its own", said)
    for item in alive:
        if item["pid"] not in run["processes"]: continue
        left = _stop(item)
        if left: return _fell_back(record, key, entry, "stopping its processes", left, said)
        steps.append(f"stopped its process {item['pid']}")
    notifications = None
    if (entry.get("notifications") or {}).get("restore") == "pending":
        outcome = devices.restore_for_recovery(key, entry, said)
        if outcome and outcome.get("restore") == "done":
            notifications = {**outcome, "silence": PUT_BACK,
                             "detail": "Queuekeeper put them back when it recovered a run whose process had ended; "
                                       f"`qk devices notifications silence {key}` silences them again"}
            steps.append("put the owner's notifications back")
        else:
            detail = (outcome or {}).get("restoreDetail") or "the platform could not put them back"
            notifications = {"lastRestoreFailure": detail, "putBackTriedAt": now()}
            steps.append(f"could not put the owner's notifications back ({detail}); the restore stays pending for "
                         f"`qk devices notifications restore {key}` or the release")
    # Apps queuekeeper opened itself, such as Shortcuts for the notification shortcuts, are its own, not the session's.
    opened = (entry.get("notifications") or {}).get("opened", [])
    theirs = [app for app in entry.get("launchedApps", []) if app not in opened]
    if theirs:
        steps.append(f"left the launched apps {', '.join(theirs)} for the holder's release to quit")
    steps.append("ended the run; the reservation stays with its holder")

    def change(current, _record):
        if condition(current) != "orphan" or proof(current) is None:
            raise Refused(f"{key} is no longer an orphan whose run is proven gone. Nothing was changed")
        ended = _run_of(current)
        entry = {name: value for name, value in current.items() if name != FALLBACK}
        entry.update(activeRun=False, run={
            **ended, "endedAt": ended.get("endedAt") or now(),
            "outcome": ended.get("outcome") or "its process ended without recording the run's end"})
        entry[RECOVERIES] = [*current.get(RECOVERIES, []), {
            "at": now(), "by": "queuekeeper", "proof": shown, "steps": steps,
            "hostProcessIdentifier": current.get("hostProcessIdentifier"), "startedAt": ended.get("startedAt")}][-KEPT:]
        if notifications:
            entry["notifications"] = {**current.get("notifications", {}), **notifications}
        return entry
    try:
        done = _settle(record, key, entry, change)
    except Refused:
        return None
    if done is not None:
        said(f"devices: recovered {key}'s run by itself, since {shown}: {'; '.join(steps)}")
    return done


def _tell(title, body):
    """Tell the owner, by a banner on this Mac; how it went."""
    from queuekeeper_mac_desktop import owner_notice
    return owner_notice.tell(title, body)


def restore_overdue(record, key, said, at=None):
    """Put the owner's notifications back on key's held device when they stayed silenced GRACE past its expected
    release (overdue), record it and tell him; the entry, or None when nothing was due."""
    entry = record.entry(key) or {}
    if not overdue(entry, at): return None
    holder, release = entry.get("ownerChatID"), entry.get("expectedReleaseBy")
    first = not (entry.get("notifications") or {}).get("putBackTriedAt")
    outcome = devices.restore_for_recovery(key, entry, said) or {}
    minutes = f"{grace() / 60:g} minutes"
    why = f"held by {holder} more than {minutes} past its expected release, {release}, with the restore pending"
    if outcome.get("restore") == "done":
        title = f"Queuekeeper put back your notifications on the {key}"
        body = (f"The {key} was {why}. Queuekeeper ran the restoring shortcut, which turns Do Not Disturb off if the "
                f"run's silencing turned it on. {holder} still holds the {key}.")
        value = {**outcome, "silence": PUT_BACK, "putBackTriedAt": now(),
                 "putBack": {"by": "queuekeeper", "at": now(), "why": why, "told": _tell(title, body)},
                 "detail": f"Queuekeeper put them back {minutes} past the expected release; "
                           f"`qk devices notifications silence {key}` silences them again"}
        said(f"devices: put back the owner's notifications on {key}, {why}; told the owner "
             f"({value['putBack']['told']}). {holder} still holds it")
    else:
        detail = outcome.get("restoreDetail") or "the platform could not put them back"
        told = _tell(f"Queuekeeper could not put back your notifications on the {key}",
                     f"The {key} was {why}, and its restoring shortcut failed: {detail}. Do Not Disturb may still be "
                     "on.") if first else None
        value = {"lastRestoreFailure": detail, "putBackTriedAt": now(), **({"failureTold": told} if told else {})}
        said(f"devices: could not put back the owner's notifications on {key}, {why}: {detail}. It is tried again in "
             f"{RETRY // 60} minutes" + (f"; told the owner ({told})" if told else ""))

    def change(current, _record):
        if (current.get("notifications") or {}).get("restore") != "pending":
            raise Refused(f"{key}'s restore is no longer pending. Nothing was changed")
        return {**current, "notifications": {**current.get("notifications", {}), **value}}
    try: return _settle(record, key, entry, change)
    except Refused: return None


class _Lock:
    """One upkeep at a time, by an flock beside the record that the kernel drops with its process."""

    def __init__(self, record, wait):
        self.path, self.wait, self.descriptor = record.path.with_name(LOCK), wait, None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline: return False
                time.sleep(0.1)

    def __exit__(self, *_):
        os.close(self.descriptor)


def upkeep(record=None, said=None, keys=None, at=None, wait=WAIT):
    """Put right what is due in record (due), of keys' entries or every one; what was said of it. Another upkeep
    running is waited for up to wait seconds, then this one goes on without doing anything."""
    record = record or devices.Record()
    lines = []
    tell = lambda text: (lines.append(text), (said or (lambda _text: None))(text))
    try: value = record.load()
    except (OSError, Refused) as error: return [f"devices: the device record could not be read: {error}"]
    todo = [(key, kind) for key, kind in due(value, at) if keys is None or key in keys]
    if not todo: return lines
    with _Lock(record, wait) as held:
        if not held: return lines
        for key, kind in todo:
            try:
                if kind == "run": recover_run(record, key, tell)
                else: restore_overdue(record, key, tell, at)
            except (OSError, Refused, TimeoutError, ValueError) as error:
                tell(f"devices: Queuekeeper could not put {key} right by itself: {error}")
    return lines


def spawn():
    """Start an upkeep apart from this process when something in this Mac's record is due and none was started
    within EVERY seconds (plugins.HOST_COMMAND). It returns at once, and never raises."""
    try:
        if os.environ.get(SWITCH) == "off": return
        record = devices.Record()
        if not due(record.load()): return
        stamp = record.path.with_name(STAMP)
        try:
            if time.time() - stamp.stat().st_mtime < EVERY: return
        except FileNotFoundError:
            pass
        stamp.touch()  # before the upkeep starts, so that commands started together start one
        path = os.pathsep.join(entry for entry in sys.path if entry)
        started = subprocess.Popen([sys.executable, "-m", __name__], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                   cwd="/", env={**os.environ, "PYTHONPATH": path})
        import threading
        threading.Thread(target=started.wait, daemon=True).start()
    except Exception:  # noqa: BLE001 -- upkeep never stops the command that starts it
        return


def main():
    from . import platforms
    if not platforms.PLATFORMS: platforms.discover()
    upkeep(said=lambda text: print(text, file=sys.stderr, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
