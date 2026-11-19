"""The visibility features and their switch: `qk visibility`.

A feature shows the queue to its owner in some way other than the command line and log files: a notification, a
web page. Each is a module or package of its own in this one, found by its name, so adding one changes no other
file. A feature:

  - has a `register()`, which the command calls only while the feature is on (extensions.load), and which adds
    the feature's commands to the core as an extension does;
  - reads the queue only through its journal (events) and its snapshot (merge_queue.snapshot);
  - imports the core, and never another feature. The core imports none of them
    (tests/architecture/test_queuekeeper_imports.py).

Every feature is off until its owner turns it on. What is on is the owner's and this Mac's, not a project's, so
it is kept beside the admission log, in visibility.json, and not in queuekeeper.json. QUEUEKEEPER_VISIBILITY=off
turns every feature off for the commands that see it, whatever the file says.
"""
import argparse
import ast
import importlib.util
import json
import os
from importlib.metadata import entry_points
import sys

from .. import extensions, resources

FILE, SWITCH, OFF = "visibility.json", "QUEUEKEEPER_VISIBILITY", "off"


def available():
    """Each feature here as name -> its module's name, without importing any."""
    found = {entry.name: entry.value for entry in entry_points(group="queuekeeper.visibility")}
    # Checkout launchers expose source roots without installing distribution metadata.
    from ..checkout import components
    for module, item in components().items():
        if item.get("visibility") and importlib.util.find_spec(module) is not None:
            found.setdefault(item["visibility"], module)
    return found


def described(name):
    """The first line of a feature's docstring, read from its source and not by importing it."""
    try:
        with open(importlib.util.find_spec(available()[name]).origin) as source:
            return (ast.get_docstring(ast.parse(source.read())) or "").split("\n")[0]
    except Exception:  # noqa: BLE001
        return ""


def settings_path():
    return resources.log_path().parent / FILE


def chosen():
    """What the owner turned on or off, as name -> bool. A file that cannot be read turns nothing on."""
    features = settings().get("features")
    return {name: on for name, on in features.items() if isinstance(on, bool)} if isinstance(features, dict) else {}


def switched_off():
    return os.environ.get(SWITCH, "").lower() == OFF


def enabled():
    """The names of the features that are on, in order: none while QUEUEKEEPER_VISIBILITY is off."""
    if switched_off(): return []
    choices = chosen()
    return [name for name in sorted(available()) if choices.get(name)]


def settings():
    """The owner's settings file as an object: {} for one that is missing or cannot be read. `features` is the
    switch's; a feature keeps its own settings under its own name."""
    try: found = json.loads(settings_path().read_text())
    except (OSError, ValueError): return {}
    return found if isinstance(found, dict) else {}


def section(found, name):
    """The object found holds under name, made there when it holds none or something else."""
    if not isinstance(found.get(name), dict): found[name] = {}
    return found[name]


def update(change):
    """Apply change to the owner's settings and write them, keeping whatever else the file holds."""
    found = settings()
    change(found)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(json.dumps(found, indent=2) + "\n")
    os.replace(staged, path)


def switch(name, on):
    """Turn the feature name on or off in the owner's settings."""
    if name not in available():
        raise ValueError(f"no visibility feature is named {name}: " + (", ".join(sorted(available())) or "none is installed"))
    update(lambda found: section(found, "features").__setitem__(name, on))


def load(say=lambda words: print(words, file=sys.stderr)):
    """Register each feature that is on. One that cannot be imported or registered is said and left out: a broken
    feature costs its owner that feature, and never the command."""
    for name in enabled():
        try: extensions.load([available()[name]])
        except Exception as error:  # noqa: BLE001
            say(f"queuekeeper: the visibility feature {name} did not load, and is left out: {error}; "
                f"`qk visibility disable {name}` turns it off")


def status(as_json=False, out=print):
    names, choices, off = available(), chosen(), switched_off()
    if as_json:
        out(json.dumps({"settings": str(settings_path()), "switchedOff": off,
                        "features": {name: {"enabled": bool(choices.get(name)), "description": described(name)}
                                     for name in sorted(names)}}, indent=2))
        return 0
    if off: out(f"{SWITCH}={OFF}: every feature is off for this command, whatever is chosen below")
    if not names: out("no visibility feature is installed")
    for name in sorted(names):
        out(f"{'on ' if choices.get(name) else 'off'}  {name}" + (f"  {described(name)}" if described(name) else ""))
    out(f"settings: {settings_path()}")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="qk visibility", description="turn the features that show the "
                                     "queue to its owner on and off, for this account on this Mac")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("status", help="list each feature and whether it is on").add_argument("--json", action="store_true")
    operations.add_parser("enable", help="turn a feature on").add_argument("feature")
    operations.add_parser("disable", help="turn a feature off").add_argument("feature")
    args = parser.parse_args(argv)
    if args.operation == "status": return status(args.json)
    try: switch(args.feature, args.operation == "enable")
    except ValueError as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 2
    print(f"{args.feature} is {'on' if args.operation == 'enable' else 'off'}")
    return 0
