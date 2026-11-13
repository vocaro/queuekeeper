"""A release's physical-device pass as a lane: `qk device-pass --device KEY ... -- COMMAND` (vocaro-engineering#148).

The release gate's device pass (guides/releasing.md, Two gates) is a lane of its own, named in
queuekeeper.json's `release.lanes`, whose command is this one, or a project script that builds first and
then calls run() from Python. `qk run LANE` admits it and writes its receipt, so `eng release` finds a pass
of it at the commit as it finds any other lane's (release.lane_passed). The lane is heavy and says
`"here"`: physical devices are reserved in this Mac's device record only, and the second Mac's defers them.

For each device named, in the order named, the pass:

1. uses the device's entry when the session already holds it, with no run active, and leaves it held
   afterward; or else takes it in turn, first come first served (`devices take`), with the owner's
   notifications silenced, and releases it afterward, putting them back. A device whose turn did not come
   within --wait is not run. The pass checks no approval: a session runs it on the owner's iPhone only
   when the owner has agreed, which is between the session and the owner (owner's decision, 2026-11-06);
2. runs COMMAND once on it, as `devices run` does: bounded by the reservation, refused when it names any
   other device, and watched for a notification banner, after which it runs once more. `{udid}` and
   `{key}` in COMMAND's words become the device's hardware UDID and its entry's key, and the command finds
   them in QUEUEKEEPER_DEVICE_UDID and QUEUEKEEPER_DEVICE_KEY too, with its own results directory in QUEUEKEEPER_RESULTS_DIR when
   the lane's run gives one.

The pass exits 0 when every device's command exited 0; 1 when any failed; and otherwise UNMEASURED (3), so
that a device not run, or a run a notification reached twice or that its bound stopped, leaves the lane
unmeasured, never passed (guides/evidence.md, Honest outcome states). It writes each device's outcome to
device-pass.json in the results directory, by key, with no device identity. A device it took and could not
release stays held, said with the command that releases it, and the pass exits 2.
"""
import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from . import devices
from .. import processes
from ..lane_results import UNMEASURED
from ..parallel import RESULTS_VARIABLE

PASSED, FAILED, NOT_RUN, UNMEASURED_PART = "passed", "failed", "not run", "unmeasured"
# The variables the command finds its device in.
KEY_VARIABLE, UDID_VARIABLE = "QUEUEKEEPER_DEVICE_KEY", "QUEUEKEEPER_DEVICE_UDID"
# Minutes a device the pass takes is reserved for, unless --minutes says otherwise: its expected release,
# which bounds the command's run.
MINUTES = 60
# Seconds the pass waits in a device's line, unless --wait says otherwise, as `devices take` does.
WAIT = 3600
SUMMARY = "device-pass.json"


def passed(results, keys):
    """Whether this run's summary records a pass on every device in the lane's plan."""
    try:
        summary = json.loads((Path(results) / SUMMARY).read_text())
        parts = summary["devices"]
        return (summary["status"] == 0 and len(parts) == len(set(keys))
                and {part["key"] for part in parts if part["outcome"] == PASSED} == set(keys))
    except (OSError, ValueError, KeyError, TypeError):
        return False


class Misplaced(RuntimeError):
    """The pass runs where it can reserve no physical device: on the second Mac, whose record defers them."""


@dataclass
class Part:
    """One device's part of the pass."""
    key: str
    outcome: str
    detail: str
    status: int = None
    took: bool = False
    released: bool = None


def words(command, key, udid):
    return [word.replace("{udid}", udid).replace("{key}", key) for word in command]


def acquire(record, key, owner, *, purpose, minutes, wait, said):
    """key's entry for the run and whether the pass took it, or a Part saying why it is not run."""
    value = record.load()
    if value.get(devices.DEFERRED):
        raise Misplaced(f"this Mac's device record defers physical devices to {value[devices.DEFERRED]}, so the pass "
                        "runs there: give the lane \"here\" with the reason, a physical device, so that qk run keeps "
                        "it on the Mac whose record holds them (guides/releasing.md, The device pass)")
    entry = value["devices"].get(key)
    if entry is not None and devices.condition(entry) == "held" and entry.get("ownerChatID") == owner:
        said(f"device-pass: {key}: {owner} holds it already, so the pass uses that reservation and leaves it held")
        return entry, False
    release_by = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    try:
        entry = record.take(key, owner=owner, purpose=purpose, release_by=release_by, timeout=wait, said=said)
    except (TimeoutError, devices.Refused, ValueError) as error:
        return Part(key, NOT_RUN, str(error)), False
    entry = devices.silence_notifications(record, key, owner=owner, revision=entry["revision"], said=said)
    return entry, True


def execute(record, key, owner, entry, command, *, limit, cwd, results, said, launched=()):
    """Run command on key's device under a bounded run; the device's Part. launched, the bundle identifiers of
    the apps the command launches there, joins the entry's launched apps first, so its release says how they
    were quit."""
    udid = entry["device"].upper()
    command = words(command, key, udid)
    try:
        if launched: entry = record.update(key, owner=owner, revision=entry["revision"], launched=list(launched))
        run = devices.BoundedRun([key], owner, command, record=record, revision=entry["revision"], limit=limit,
                                 root=cwd)
    except devices.Refused as error:
        return Part(key, NOT_RUN, str(error))
    environment = {**os.environ, KEY_VARIABLE: key, UDID_VARIABLE: udid}
    if results is not None:
        directory = Path(results) / key
        directory.mkdir(parents=True, exist_ok=True)
        environment[RESULTS_VARIABLE] = str(directory)
    notified = (record.entry(key).get("notifications") or {}).get("silence") or "nothing was done"
    extra = {"notifications": notified, "purpose": "device pass"}
    said(f"device-pass: {key}: running {command[0]}")
    try:
        code = devices.watched_run(run, key, command, extra, said, cwd=cwd, env=environment)
    except processes.TimedOut as error:
        return Part(key, UNMEASURED_PART, f"its bound stopped it: {error}")
    except devices.Unmeasured:
        return Part(key, UNMEASURED_PART, "a notification reached its run and its rerun")
    except processes.NotStarted as error:
        return Part(key, FAILED, f"the command did not start: {error}", 2)
    code = 128 - code if code < 0 else code
    if code == 0: return Part(key, PASSED, "exit 0", 0)
    if code == UNMEASURED: return Part(key, UNMEASURED_PART, f"exit {UNMEASURED}, unmeasured", code)
    return Part(key, FAILED, f"exit {code}", code)


def release(record, key, owner, part, *, apps_quit, said):
    """Release a device the pass took, putting the owner's notifications back first; whether it was released."""
    try:
        entry = record.entry(key)
        revision = devices.restore_notifications(record, key, owner=owner, revision=entry["revision"],
                                                 said=said)["revision"]
        record.release(key, owner=owner, revision=revision, outcome=f"device pass: {part.outcome} ({part.detail})",
                       restoration="the command's own restoration; the owner's notifications put back",
                       apps_quit=apps_quit)
    except (devices.Refused, OSError, ValueError, TimeoutError) as error:
        said(f"device-pass: {key} stays held by {owner}: {error}. Release it with `devices release {key} --owner "
             f"{owner} --revision N --outcome … --restoration …` once that is settled")
        return False
    said(f"device-pass: released {key}")
    return True


def status(parts):
    """The pass's exit status: 1 when a device failed, UNMEASURED when one is not run or unmeasured, else 0."""
    outcomes = {part.outcome for part in parts}
    if not parts: return 2
    if FAILED in outcomes: return 1
    if outcomes - {PASSED}: return UNMEASURED
    return 0


def run(keys, command, *, owner=None, purpose="release device pass", minutes=MINUTES, wait=WAIT, limit=None,
        apps_quit=None, launched=(), cwd=None, results=None, record=None, said=None):
    """Run command on each device keys names, in turn; each device's Part and the pass's status.

    command is a list of words, or a function of (key, udid) that returns them, for a project script that
    gives each device its own steps. launched names the apps the command launches on each device, by bundle
    identifier, which its entry records before the run (`devices update --launched`); apps_quit then says how
    the command quits them, which a device's release records."""
    if launched and not apps_quit:
        raise ValueError("a pass whose command launches apps says how it quits them: apps_quit")
    said = said or (lambda text: print(text, file=sys.stderr, flush=True))
    keys = list(dict.fromkeys(keys))
    if not keys: raise ValueError("a device pass names at least one device: --device KEY")
    owner = owner or devices.default_owner()
    if not owner: raise ValueError(f"name the session: --owner, or {devices.OWNER}")
    record = record or devices.Record()
    results = results if results is not None else os.environ.get(RESULTS_VARIABLE)
    parts = []
    for key in keys:
        acquired, took = acquire(record, key, owner, purpose=purpose, minutes=minutes, wait=wait, said=said)
        if isinstance(acquired, Part):
            part = acquired
        else:
            given = command(key, acquired["device"].upper()) if callable(command) else command
            try:
                part = execute(record, key, owner, acquired, given, limit=limit, cwd=cwd, results=results, said=said,
                               launched=launched)
            except BaseException:
                # A stop, whose run's end the entry records: put back a device the pass took before it goes on.
                # A run whose end is unconfirmed stays active, and the release refuses and says so.
                if took: release(record, key, owner, Part(key, FAILED, "the pass was stopped"), apps_quit=apps_quit,
                                 said=said)
                raise
            part.took = took
            if took: part.released = release(record, key, owner, part, apps_quit=apps_quit, said=said)
        said(f"device-pass: {key}: {part.outcome}: {part.detail}")
        parts.append(part)
    code = status(parts)
    if any(part.took and part.released is False for part in parts): code = 2
    if results is not None:
        Path(results).mkdir(parents=True, exist_ok=True)
        (Path(results) / SUMMARY).write_text(json.dumps({"status": code, "devices": [asdict(part) for part in parts]},
                                                        indent=2) + "\n")
    return parts, code


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk device-pass", description="Run a release's device pass: COMMAND "
        "once on each physical device named, in turn, under its reservation (guides/releasing.md, The device pass)")
    parser.add_argument("--device", action="append", required=True, metavar="KEY", choices=sorted(devices.PAIRED),
                        help="a paired device's entry; repeat for each, in the order to run them")
    parser.add_argument("--owner", default=devices.default_owner(), help=f"the session (default: {devices.OWNER}, "
                        "else the agent session)")
    parser.add_argument("--purpose", default="release device pass", help="the reservation's purpose")
    parser.add_argument("--minutes", type=float, default=MINUTES, help="how long a device the pass takes is "
                        f"reserved for, which bounds its run (default {MINUTES})")
    parser.add_argument("--wait", type=float, default=WAIT, metavar="SECONDS", help="how long to wait in a device's "
                        f"line before leaving it not run (default {WAIT})")
    parser.add_argument("--limit", type=float, metavar="SECONDS", help="stop each device's run after at most this long")
    parser.add_argument("--launched", action="append", default=[], metavar="BUNDLE", help="an app the command "
                        "launches on each device, which its entry records before the run (repeatable)")
    parser.add_argument("--apps-quit", help="how the command quits the apps it launches, which a device's release "
                        "records; required with --launched")
    argv = list(argv)
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command: parser.error("name the command after --: qk device-pass --device KEY -- COMMAND")
    try:
        with processes.forwarding_signals():
            _parts, code = run(args.device, command, owner=args.owner, purpose=args.purpose, minutes=args.minutes,
                               wait=args.wait, limit=args.limit, apps_quit=args.apps_quit, launched=args.launched,
                               cwd=Path(project).resolve() if project else None)
        return code
    except processes.Interrupted:
        print("device-pass: stopped", file=sys.stderr)
        return 130
    except (Misplaced, OSError, ValueError, RuntimeError) as error:
        print(f"device-pass: {error}", file=sys.stderr)
        return 2
