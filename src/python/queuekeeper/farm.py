"""The farm: the Macs beside this one that take work, as their owner lists them.

One file on the Mac that sends work, farm.json beside its admission log, names each Mac of the farm: the name
queuekeeper calls it by, the SSH destination that reaches it, its hardware UUID and whether it takes work now. The
owner adds a Mac with `qk farm add`, renames it, pauses it while it is out for a while, and removes it for good;
nothing else names a Mac, neither an SSH alias queuekeeper looks for nor a variable. A Mac's name is its own, the
local host name it gave when it was added unless the owner chose another, and can change: its hardware UUID is
what a record keeps, so that a renamed Mac's history stays its own. The Macs are tried in the file's order.

A HEAVY_SLOT_DIR namespace keeps its own farm.json, as it keeps its own admission log, so a test's farm is its own.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
import re
import sys

from . import offload_host, trial

FILE = "farm.json"
VERSION = 1
ACTIVE, PAUSED = "active", "paused"
# A Mac's name: what a local host name may be, so that the name a Mac gives of itself serves.
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
# Words a command already gives another meaning.
RESERVED = {"here", "this", "none", "off", "all"}
# An SSH destination, as ssh takes it: an alias of ~/.ssh/config, a host name or USER@HOST. Never an option.
DESTINATION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*")


def path():
    return offload_host.admission_directory() / FILE


def read():
    """The farm's Macs, in the order they are tried: each {"id", "name", "ssh", "state", "added"}, and
    "pausedBecause" while paused. No file is no Mac. A file that is not a farm stops whatever reads it, with what is
    wrong, rather than sending work past the Macs it names. A trial's farm has no Mac (trial.py)."""
    if trial.active(): return []
    try:
        found = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ValueError(f"{path()} cannot be read as the farm's Macs: {error}") from None
    macs = found.get("macs") if isinstance(found, dict) else None
    if not isinstance(macs, list) or found.get("version") != VERSION:
        raise ValueError(f"{path()} is not a version {VERSION} farm: write it with `qk farm add`")
    for mac in macs:
        if not (isinstance(mac, dict) and isinstance(mac.get("name"), str) and NAME.fullmatch(mac["name"])
                and isinstance(mac.get("ssh"), str) and DESTINATION.fullmatch(mac["ssh"])
                and mac.get("state") in (ACTIVE, PAUSED)):
            raise ValueError(f"{path()} holds a Mac without a name, an SSH destination or a state: {mac!r}")
    if len({mac["name"].lower() for mac in macs}) != len(macs):
        raise ValueError(f"{path()} names two Macs alike")
    return macs


def active():
    """The Macs that take work now, in order."""
    return [mac for mac in read() if mac["state"] == ACTIVE]


def destinations():
    """The SSH destinations of the Macs that take work now, in order."""
    return tuple(mac["ssh"] for mac in active())


def foreground():
    """The SSH destinations of the Macs a foreground lane, which takes a Mac's screen, may go to, in order: the Macs
    that take work now and that the owner marked for it (`qk farm foreground NAME`), or, while none is marked, the
    first that takes work now."""
    found = active()
    marked = tuple(mac["ssh"] for mac in found if mac.get("foreground"))
    return marked or tuple(mac["ssh"] for mac in found[:1])


def find(name, macs=None):
    """The Mac of that name, whatever its case."""
    macs = read() if macs is None else macs
    for mac in macs:
        if mac["name"].lower() == name.lower(): return mac
    known = ", ".join(mac["name"] for mac in macs) or "none: add one with `qk farm add DESTINATION`"
    raise ValueError(f"the farm has no Mac named {name} (its Macs: {known})")


def destination(name):
    """The SSH destination of the farm's Mac of that name; or name itself, an SSH destination, when the farm has no
    such Mac, as for a Mac the owner reaches before adding it."""
    for mac in read():
        if mac["name"].lower() == name.lower(): return mac["ssh"]
    return name


def named(host):
    """The name of the farm's Mac that the SSH destination host reaches, or host when it reaches none of them."""
    with contextlib.suppress(ValueError):
        for mac in read():
            if mac["ssh"] == host: return mac["name"]
    return host


@contextlib.contextmanager
def changing():
    """The farm's Macs, to change in place, written back whole when the block ends without raising. One process
    changes the farm at a time."""
    directory = path().parent
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "farm.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        macs = read()
        yield macs
        partial = path().with_suffix(".partial")
        partial.write_text(json.dumps({"version": VERSION, "macs": macs}, indent=2) + "\n", encoding="utf-8")
        os.replace(partial, path())


def checked_name(name, macs, but=None):
    if not NAME.fullmatch(name) or name.lower() in RESERVED:
        raise ValueError(f"{name!r} cannot name a Mac: a name is letters, digits, dots, hyphens and underscores, "
                         f"begins and ends with a letter or digit, and is none of {', '.join(sorted(RESERVED))}")
    taken = next((mac for mac in macs if mac is not but and mac["name"].lower() == name.lower()), None)
    if taken is not None:
        raise ValueError(f"the farm already has a Mac named {taken['name']}, reached at {taken['ssh']}: choose "
                         "another name with --name")
    return name


def add(ssh, name=None, *, join, here=None, now=None):
    """Add the Mac that the SSH destination reaches, last in the order, and return it with what join found.

    join(ssh) asks the Mac for its hardware UUID and its own name, and has it move its earlier directory
    (offload_host.join); it raises when the Mac cannot be reached or cannot answer, and then nothing is added. The
    name is the one given, else the Mac's own. A Mac is added once: neither this Mac, whose hardware UUID is here,
    nor one the farm has under another name or destination."""
    if not DESTINATION.fullmatch(ssh):
        raise ValueError(f"{ssh!r} is not an SSH destination: name a ~/.ssh/config alias, a host name or USER@HOST")
    found = join(ssh)
    identity = found.get("id")
    if not identity:
        raise ValueError(f"{ssh} could not say which Mac it is: its hardware UUID cannot be read there")
    if here is not None and identity == here:
        raise ValueError(f"{ssh} reaches this Mac, which takes work already and is not added to its own farm")
    with changing() as macs:
        for mac in macs:
            if mac.get("id") == identity or mac["ssh"] == ssh:
                raise ValueError(f"{ssh} reaches {mac['name']}, which the farm has already, reached at {mac['ssh']}")
        mac = {"id": identity, "name": checked_name(name or found.get("name") or "", macs), "ssh": ssh,
               "state": ACTIVE, "added": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        macs.append(mac)
    return mac, found


def rename(old, new):
    with changing() as macs:
        mac = find(old, macs)
        mac["name"] = checked_name(new, macs, but=mac)
    return mac


def pause(name, reason=None):
    """Stop offering the Mac work, until it is resumed. What runs there runs on."""
    with changing() as macs:
        mac = find(name, macs)
        mac["state"] = PAUSED
        mac.pop("pausedBecause", None)
        if reason: mac["pausedBecause"] = reason
    return mac


def resume(name):
    with changing() as macs:
        mac = find(name, macs)
        mac["state"] = ACTIVE
        mac.pop("pausedBecause", None)
    return mac


def mark_foreground(name, on=True):
    """Mark the Mac as one whose screen a foreground lane may take, or with on False as one it may not."""
    with changing() as macs:
        mac = find(name, macs)
        mac.pop("foreground", None)
        if on: mac["foreground"] = True
    return mac


def remove(name, *, running=lambda ssh: []):
    """Take the Mac out of the farm for good: nothing is offered to it and nothing reports it from then on. What
    earlier runs recorded of it stays. running(ssh) names the runs this Mac still has there, and while it names
    any, the Mac stays."""
    with changing() as macs:
        mac = find(name, macs)
        held = running(mac["ssh"])
        if held:
            raise ValueError(f"{mac['name']} still runs work this Mac sent it ({', '.join(held)}): remove it once "
                             f"that has ended, and pause it meanwhile with `qk farm pause {mac['name']}`")
        macs.remove(mac)
    return mac


def line(mac):
    state = mac["state"] + (f" ({mac['pausedBecause']})" if mac.get("pausedBecause") else "")
    return f"{mac['name']}  {state}  ssh {mac['ssh']}" + ("  foreground" if mac.get("foreground") else "")


def main(argv):
    """`qk farm`: list the farm's Macs, add one, rename, pause, resume or remove one, or mark one for foreground
    lanes. It reads no project."""
    parser = argparse.ArgumentParser(prog="qk farm", description="the Macs beside this one that take work")
    operations = parser.add_subparsers(dest="operation")
    listing = operations.add_parser("list", help="the farm's Macs, in the order work is offered to them")
    listing.add_argument("--json", action="store_true")
    adding = operations.add_parser("add", help="add the Mac an SSH destination reaches, after reaching it")
    adding.add_argument("ssh", metavar="DESTINATION", help="a ~/.ssh/config alias, a host name or USER@HOST")
    adding.add_argument("--name", help="what to call the Mac (default: the local host name it gives)")
    renaming = operations.add_parser("rename", help="call a Mac by another name; its records stay its own")
    renaming.add_argument("name")
    renaming.add_argument("new")
    pausing = operations.add_parser("pause", help="offer a Mac no work until it is resumed")
    pausing.add_argument("name")
    pausing.add_argument("--reason")
    operations.add_parser("resume", help="offer a paused Mac work again").add_argument("name")
    operations.add_parser("remove", help="take a Mac out of the farm for good").add_argument("name")
    marking = operations.add_parser("foreground", help="let foreground lanes take a Mac's screen, or with --off not")
    marking.add_argument("name")
    marking.add_argument("--off", action="store_true")
    args = parser.parse_args(argv)
    # Reaching a Mac is remote's, which reads the farm: imported here, where a command needs it.
    from . import remote
    try:
        if args.operation in (None, "list"):
            macs = read()
            if getattr(args, "json", False):
                print(json.dumps({"macs": macs}, indent=2))
            elif not macs:
                print("The farm has no Mac beside this one: add one with `qk farm add DESTINATION`")
            else:
                for mac in macs: print(line(mac))
            return 0
        if trial.active(): raise ValueError(trial.refusal(f"qk farm {args.operation}"))
        if args.operation == "add":
            mac, found = add(args.ssh, args.name, join=remote.join, here=offload_host.hardware_id())
            print(f"Added {line(mac)}")
            if found.get("moved"): print(f"Moved {found['from']} there to {found['to']}, with its checkouts")
            if found.get("linked"):
                print(f"Linked {found['linked']} there to it, for build state that still names the old path")
            for problem in found.get("unrepaired", []): print(f"A checkout there was not repaired: {problem}")
            if found.get("left"): print(f"Not moved there: {found['left']}")
            print(f"Next, give it the tools the lanes need: `qk remote setup --host {mac['name']}`")
        elif args.operation == "rename":
            mac = rename(args.name, args.new)
            print(f"{args.name} is now {mac['name']}")
        elif args.operation == "pause":
            print(f"Paused {line(pause(args.name, args.reason))}")
        elif args.operation == "resume":
            print(f"Resumed {line(resume(args.name))}")
        elif args.operation == "foreground":
            mac = mark_foreground(args.name, not args.off)
            taken = ", ".join(named(ssh) for ssh in foreground()) or "no Mac"
            print(f"{line(mac)}\nForeground lanes now go to: {taken}")
        elif args.operation == "remove":
            mac = remove(args.name, running=remote.running_on)
            print(f"Removed {mac['name']}. Its checkouts stay on it, under ~/{remote.ROOT}, until they are deleted there")
        return 0
    except remote.Unavailable as error:
        fix = getattr(error, "fix", None)
        print(f"qk farm: {getattr(error, 'problem', error)}" + (f"\nqk farm: to fix it, {fix}" if fix else ""),
              file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"qk farm: {error}", file=sys.stderr)
        return 2
