"""`queuekeeper notify`: what is on, a notice to try the channels with, and the switches for one rule or channel."""
import argparse
import json
from pathlib import Path
import sys
import time

from ... import git_state, merge_queue, visibility
from . import HELP, channels, check, deliver, rules

# A value shown in place of one that may be a secret, such as a webhook's address or a header.
HIDDEN = "(set)"


def status(as_json=False, out=print):
    found = deliver.settings()
    on = rules.chosen(found)
    states = {name: channels.state(found, name) for name in channels.available()}
    if as_json:
        out(json.dumps({"settings": str(visibility.settings_path()), "log": str(deliver.log_path()),
                        "rules": {name: {"enabled": name in on, "description": RULE[1]} for name, RULE in rules.RULES.items()},
                        "channels": {name: {"enabled": off is None, "off": off, "keys": sorted(config)}
                                     for name, (_, config, off) in states.items()}}, indent=2))
        return 0
    out("rules")
    for name, (_, described) in rules.RULES.items(): out(f"  {'on ' if name in on else 'off'}  {name}  {described}")
    out("channels")
    for name, (module, config, off) in states.items():
        described = ((module.__doc__ or "").split(".")[0].replace("\n", " ") if module else "")
        out(f"  {'on ' if off is None else 'off'}  {name}  {described}" + (f"\n       off: {off}" if off else ""))
        for key in sorted(config):
            if key != "enabled": out(f"       {key}: {HIDDEN}")
    out(f"settings: {visibility.settings_path()}, under \"notify\"")
    out(f"log: {deliver.log_path()}")
    return 0


def switch(name, on):
    """Turn the rule or channel name on or off in the owner's settings."""
    if name in rules.RULES:
        visibility.update(lambda found: visibility.section(visibility.section(found, "notify"), "rules")
                          .__setitem__(name, on))
    elif name in channels.available():
        visibility.update(lambda found: visibility.section(visibility.section(
            visibility.section(found, "notify"), "channels"), name).__setitem__("enabled", on))
    else:
        raise ValueError(f"notify has no rule or channel named {name}: the rules are {', '.join(rules.RULES)}, and the "
                         f"channels {', '.join(channels.available())}")


def assign(channel, key, value=None):
    """Set a channel's setting, or with no value remove it. A value that reads as JSON is taken as JSON, so that an
    argument list or a number can be given; any other is text."""
    if channel not in channels.available():
        raise ValueError(f"notify has no channel named {channel}: {', '.join(channels.available())}")
    if key == "enabled": raise ValueError(f"`queuekeeper notify enable {channel}` and `disable` turn a channel on and off")
    def change(found):
        config = visibility.section(visibility.section(visibility.section(found, "notify"), "channels"), channel)
        if value is None: config.pop(key, None)
        else:
            try: config[key] = json.loads(value)
            except ValueError: config[key] = value
    visibility.update(change)


def test(repository, only=None, out=print):
    """Send a notice that says it is a test through each channel that is on, or through only, and say how each
    went: 0 when every one sent it, 1 when one refused it or none is on."""
    found = deliver.settings()
    notice = {"rule": "test", "level": rules.NORMAL, "title": "Queuekeeper test notice",
              "body": "Notifications from this queue reach you here", "repository": repository,
              "event": {"v": 1, "id": "test", "time": round(time.time(), 3), "kind": "notify.test"}}
    names = [only] if only else channels.available()
    failed = sent = 0
    for name in names:
        if name not in channels.available():
            raise ValueError(f"notify has no channel named {name}: {', '.join(channels.available())}")
        module, config, off = channels.state(found, name)
        if off is not None:
            out(f"{name}: off: {off}")
            failed += bool(only)
            continue
        try:
            module.send(notice, config)
            out(f"{name}: sent")
            sent += 1
        except Exception as error:  # noqa: BLE001
            out(f"{name}: did not send: {error}")
            failed += 1
    if not sent and not failed: out("no channel is on, so the test went nowhere")
    return 0 if sent and not failed else 1


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="queuekeeper notify", description=HELP)
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("status", help="list each rule and channel, and whether it is on").add_argument(
        "--json", action="store_true")
    operations.add_parser("enable", help="turn a rule or a channel on").add_argument("name")
    operations.add_parser("disable", help="turn a rule or a channel off").add_argument("name")
    setting = operations.add_parser("set", help="set one of a channel's settings, such as `webhook url URL` or "
                                    "`command argv '[\"PROGRAM\"]'`; a VALUE that reads as JSON is taken as JSON")
    for name in ("channel", "key", "value"): setting.add_argument(name)
    removing = operations.add_parser("unset", help="remove one of a channel's settings")
    for name in ("channel", "key"): removing.add_argument(name)
    trying = operations.add_parser("test", help="send a test notice through each channel that is on")
    trying.add_argument("--channel", help="through this channel alone")
    operations.add_parser("check", help="look now for a queue that is stuck, as `queuekeeper queue doctor` does, and "
                          "tell of what has lasted, as the commands that reach the hosts do by themselves every two "
                          "minutes: 0 when nothing is found, 1 when something is")
    operations.add_parser("deliver", help="tell now of what this project's queue has journaled since the last "
                          "delivery, as the queue does by itself after each event")
    args = parser.parse_args(argv)
    root = Path(project or ".").resolve()
    try:
        if args.operation == "status": return status(args.json)
        if args.operation in ("enable", "disable"):
            switch(args.name, args.operation == "enable")
            print(f"{args.name} is {'on' if args.operation == 'enable' else 'off'}")
            return 0
        if args.operation in ("set", "unset"):
            assign(args.channel, args.key, getattr(args, "value", None))
            print(f"{args.channel}'s {args.key} is {'set' if args.operation == 'set' else 'removed'}")
            return 0
        # Named as a delivery names it: by the repository's main checkout, not by a linked worktree of it.
        if args.operation == "test": return test((git_state.main_checkout(root) or root).name, args.channel)
        if args.operation == "check":
            findings, told = check.check(git_state.main_checkout(root) or root)
            for finding in findings: print(f"{finding['signal']}: {finding['diagnosis']}")
            print(f"{len(findings)} finding(s), {told} told of now")
            return 1 if findings else 0
        told = deliver.deliver(merge_queue.Queue(root).directory)
        print(f"{told} notice(s) sent")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 2
