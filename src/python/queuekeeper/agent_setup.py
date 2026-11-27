"""`qk agents`: tell a project's coding agents how their work reaches main, in one command and undone by another.

Setup writes a rule the user guide gives under "Tell your agents" into the project's agent instructions, between
two marker lines: the merge queue's, or, in a project whose queuekeeper.json says `"integration": "pull-request"`,
the pull request's (vocaro/queuekeeper#374). It merges Claude Code hooks into .claude/settings.json: one at
SessionStart that reports this worktree's entries that came back or wait (agent_hooks.session_start), and optionally
a PreToolUse guard that refuses landing on main by hand (agent_hooks.pre_tool_use).

The begin marker records a hash of what was written. Setup run again rewrites a block that still matches it, so the
rule follows Queuekeeper's and the project's integration; removal deletes only such a block, never one a person
edited since. Removal takes out of settings.json only the hook commands setup writes, and leaves every other entry as it was.

Claude Code runs a hook in the session's current directory, which a Bash `cd` moves, so a launcher path relative to the
project is written anchored on $CLAUDE_PROJECT_DIR (vocaro/queuekeeper#275). Check reports a hook written before that, and setup run
again anchors it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from .agent_hooks import DIRECT_MAIN, PULL_REQUEST, integration

# The rules, word for word the blocks under "Tell your agents" in the user guide (a test keeps them equal): RULE for
# a project that lands through the merge queue, PULL_REQUEST_RULE for one that integrates through pull requests.
RULE = """\
Never push to main. Commit on your own branch, then run `qk land --wait` as a
background command and follow it to its result. Exit 0 means landed, 1 means it came back
with a reason, and 75 means the queue is held or still waiting. If the wait is interrupted, the
entry still lands: follow it with `qk queue wait ID`, or run the same `qk land --wait` again,
which follows the entry already queued. Run tests and builds with `qk run LANE` or
`qk admit -- COMMAND`, never bare, so that they wait their turn on the shared Macs. Run unittest
suites with `qk unittest`, never `python3 -m unittest`. Send a long run's output to a file, never
through `tail` or `head`, and trim when reading it; `qk progress` says how far a run has got.
"""
PULL_REQUEST_RULE = """\
Never push to main or merge a pull request. Commit on your own branch, push it and open a
pull request; a person reviews and merges it. Run tests and builds with `qk run LANE` or
`qk admit -- COMMAND`, never bare, so that they wait their turn on the shared Macs. Run unittest
suites with `qk unittest`, never `python3 -m unittest`. Send a long run's output to a file, never
through `tail` or `head`, and trim when reading it; `qk progress` says how far a run has got.
"""
RULES = {DIRECT_MAIN: RULE, PULL_REQUEST: PULL_REQUEST_RULE}
# What each integration is called where setup and check name it.
MODES = {DIRECT_MAIN: "landing through the merge queue", PULL_REQUEST: "integrating through pull requests"}
BEGIN = "<!-- queuekeeper:agents begin sha256={} -- written by `qk agents setup`; `qk agents remove` takes it out -->"
END = "<!-- queuekeeper:agents end -->"
FENCED = re.compile(r"^<!-- queuekeeper:agents begin sha256=([0-9a-f]{16})\b[^\n]*-->\n(.*?)^<!-- queuekeeper:agents end -->\n?",
                    re.MULTILINE | re.DOTALL)
INSTRUCTIONS = ("AGENTS.md", "CLAUDE.md")
SETTINGS = Path(".claude/settings.json")
SESSION_START, PRE_TOOL_USE = "session-start", "pre-tool-use"
EVENTS = {SESSION_START: ("SessionStart", None), PRE_TOOL_USE: ("PreToolUse", "Bash")}
# A command setup wrote, under whichever launcher: `qk hook session-start`, `python3 scripts/qk hook pre-tool-use`.
OURS = re.compile(r"(?:^|[\s/])qk hook (session-start|pre-tool-use)$")
TIMEOUT = 15


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def block(body=RULE):
    return f"{BEGIN.format(digest(body))}\n{body}{END}\n"


def find_block(text):
    """(match, intact) for the fenced block in text, or (None, None) when it has none."""
    found = FENCED.search(text)
    if found is None: return None, None
    return found, digest(found.group(2)) == found.group(1)


def toplevel(directory):
    try:
        found = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.SubprocessError): return None
    return Path(found.stdout.strip()) if found.returncode == 0 and found.stdout.strip() else None


def instruction_files(root, named):
    """The files named, else the first of AGENTS.md and CLAUDE.md that exists, else a new AGENTS.md."""
    if named: return [Path(path) if Path(path).is_absolute() else root / path for path in named]
    return [next((root / name for name in INSTRUCTIONS if (root / name).exists()), root / INSTRUCTIONS[0])]


def write_rule(path, force, body=RULE):
    """Put the block of rule body in path. Returns (status, line)."""
    try: text = path.read_text() if path.exists() else ""
    except OSError as error: return 2, f"{path}: cannot read: {error}"
    found, intact = find_block(text)
    if found is None:
        updated = text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text.strip() else "") + block(body)
        said = f"{path}: added the Queuekeeper rule"
    elif not intact and not force:
        return 1, (f"{path}: the Queuekeeper block was edited since it was written, so it is left as it is; "
                   "`qk agents setup --force` replaces it")
    elif found.group(0) == block(body):
        return 0, f"{path}: the Queuekeeper rule is current"
    else:
        updated = text[:found.start()] + block(body) + text[found.end():]
        other = other_mode(found.group(2), body)
        said = f"{path}: rewrote the Queuekeeper rule" + (f", which was the rule for {MODES[other]}" if other else "")
    try: path.write_text(updated)
    except OSError as error: return 2, f"{path}: cannot write: {error}"
    return 0, said


def other_mode(written, body):
    """The integration whose rule written is, when that is not the rule body, or None."""
    return next((mode for mode, rule in RULES.items() if written == rule and rule != body), None)


def remove_rule(path):
    try: text = path.read_text()
    except FileNotFoundError: return 0, f"{path}: absent, nothing to remove"
    except OSError as error: return 2, f"{path}: cannot read: {error}"
    found, intact = find_block(text)
    if found is None: return 0, f"{path}: holds no Queuekeeper block"
    if not intact:
        return 1, (f"{path}: the Queuekeeper block was edited since it was written, so it is left in place; "
                   "delete it by hand, between its two marker lines")
    before, after = text[:found.start()], text[found.end():]
    # The blank line setup put before the block goes with it.
    if before.endswith("\n\n"): before = before[:-1]
    updated = before + after
    try:
        if updated.strip(): path.write_text(updated)
        else: path.unlink()
    except OSError as error: return 2, f"{path}: cannot write: {error}"
    return 0, f"{path}: removed the Queuekeeper rule" + ("" if updated.strip() else ", and the file, now empty")


# Claude Code runs a hook in the session's current directory, which a Bash `cd` moves, and sets this to the project.
PROJECT_DIR = '"$CLAUDE_PROJECT_DIR"'


def relative_words(words, root):
    """The indexes of the words of a launcher that name a path relative to the project: one with a slash that starts at
    neither /, ~ nor a variable, or an argument naming a file in the project. Such a launcher fails once a session
    leaves the project root (vocaro/queuekeeper#275); a bare command, found on PATH, does not."""
    return [index for index, word in enumerate(words)
            if not word.startswith(("/", "~", "$", "-"))
            and ("/" in word or (index and root is not None and (root / word).exists()))]


def anchored(launcher, root):
    """The launcher with each word relative to the project anchored on $CLAUDE_PROJECT_DIR, quoted for the shell."""
    words = shlex.split(launcher)
    relative = set(relative_words(words, root))
    return " ".join(PROJECT_DIR + "/" + shlex.quote(word.removeprefix("./")) if index in relative else shlex.quote(word)
                    for index, word in enumerate(words))


def unanchored(command, root):
    """True when a hook command of ours runs its launcher by a path relative to the session's directory."""
    launcher = command.strip().removesuffix(f" hook {OURS.search(command.strip()).group(1)}")
    try: return bool(relative_words(shlex.split(launcher), root))
    except ValueError: return False


def hook_command(launcher, name):
    return f"{launcher} hook {name}"


def read_settings(path):
    """The settings as an object, {} when the file is absent. Raises ValueError for a file that is not one."""
    try: text = path.read_text()
    except FileNotFoundError: return {}
    settings = json.loads(text) if text.strip() else {}
    if not isinstance(settings, dict): raise ValueError(f"{path} does not hold a JSON object")
    return settings


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staged.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(staged, path)


def commands_of(settings, event):
    """Each hook command of event in settings, as (group, hook)."""
    for group in (settings.get("hooks") or {}).get(event) or []:
        if not isinstance(group, dict): continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str): yield group, hook


def ours(hook):
    """Which of setup's hooks a hook entry runs, or None for anyone else's."""
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str): return None
    found = OURS.search(hook["command"].strip())
    return found.group(1) if found else None


def installed(settings, name):
    return [hook["command"] for _, hook in commands_of(settings, EVENTS[name][0]) if ours(hook) == name]


def add_hook(settings, name, launcher, root=None):
    """Add the hook name in settings unless one of ours is there already, anchoring one that an earlier setup wrote
    with a relative launcher. True when it changed settings."""
    changed = False
    for _, hook in commands_of(settings, EVENTS[name][0]):
        if ours(hook) == name and unanchored(hook["command"], root):
            hook["command"] = hook_command(launcher, name)
            changed = True
    if installed(settings, name): return changed
    event, matcher = EVENTS[name]
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict): raise ValueError("settings.json's hooks is not an object")
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list): raise ValueError(f"settings.json's hooks.{event} is not a list")
    group = {"hooks": [{"type": "command", "command": hook_command(launcher, name), "timeout": TIMEOUT}]}
    if matcher: group = {"matcher": matcher, **group}
    groups.append(group)
    return True


def remove_hooks(settings):
    """Take every hook command of ours out of settings, and the groups, events and hooks object left empty. The names
    of the hooks removed."""
    removed = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict): return removed
    for name, (event, _) in EVENTS.items():
        groups = hooks.get(event)
        if not isinstance(groups, list): continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [hook for hook in group["hooks"] if ours(hook) != name]
            if len(kept) != len(group["hooks"]): removed.append(name)
            if kept: kept_groups.append({**group, "hooks": kept})
        if kept_groups: hooks[event] = kept_groups
        else: hooks.pop(event)
    if not hooks: settings.pop("hooks")
    return removed


def setup(root, args):
    mode = integration(root)
    status, lines = 0, [f"the project is {MODES[mode]} (\"integration\" in queuekeeper.json), so agents get that rule"]
    for path in instruction_files(root, args.file):
        found, said = write_rule(path, args.force, RULES[mode])
        status = max(status, found)
        lines.append(said)
    path = root / SETTINGS
    launcher = anchored(args.launcher, root)
    if launcher != args.launcher:
        lines.append(f"the hooks run `{launcher}`, anchored on the project, since Claude Code runs a hook wherever "
                     "the session has gone")
    try:
        settings = read_settings(path)
        before = {name: installed(settings, name) for name in EVENTS}
        # A guard there already is anchored too, with or without --guard.
        added = [name for name in (SESSION_START, *([PRE_TOOL_USE] if args.guard or before[PRE_TOOL_USE] else ()))
                 if add_hook(settings, name, launcher, root)]
        if added: write_settings(path, settings)
        for name in (SESSION_START, PRE_TOOL_USE):
            if name in added and before[name]:
                lines.append(f"{path}: anchored the {EVENTS[name][0]} hook on the project: `{installed(settings, name)[0]}`")
            elif name in added: lines.append(f"{path}: added the {EVENTS[name][0]} hook `{hook_command(launcher, name)}`")
            elif installed(settings, name): lines.append(f"{path}: the {EVENTS[name][0]} hook was there already")
    except (OSError, ValueError) as error:
        status = 2
        lines.append(f"{path}: left as it is: {error}")
    for line in lines: print(line)
    if status == 0 and mode == PULL_REQUEST:
        print("commit these files on a branch and merge them through a pull request, so that every worktree has them")
    elif status == 0:
        print("commit these files on a branch and land them with `qk land --wait`, so that every worktree has them")
    return status


def remove(root, args):
    status, lines = 0, []
    for path in instruction_files(root, args.file) if args.file else [root / name for name in INSTRUCTIONS]:
        found, said = remove_rule(path)
        status = max(status, found)
        if args.file or "absent" not in said: lines.append(said)
    path = root / SETTINGS
    try:
        settings = read_settings(path)
        removed = remove_hooks(settings)
        if removed:
            if settings: write_settings(path, settings)
            else: path.unlink()
            lines.append(f"{path}: removed the {' and '.join(EVENTS[name][0] for name in removed)} hook"
                         + ("" if settings else ", and the file, now empty"))
        elif path.exists(): lines.append(f"{path}: holds no Queuekeeper hook")
    except (OSError, ValueError) as error:
        status = 2
        lines.append(f"{path}: left as it is: {error}")
    for line in lines: print(line)
    return status


def check(root, args):
    """Exit 0 when the project's integration's rule and the SessionStart hook are both in place."""
    mode, rule, wrong = integration(root), False, False
    for path in instruction_files(root, args.file) if args.file else [root / name for name in INSTRUCTIONS]:
        try: text = path.read_text()
        except OSError: continue
        found, intact = find_block(text)
        if found is None: continue
        rule = True
        current = found.group(0) == block(RULES[mode])
        if other := other_mode(found.group(2), RULES[mode]) if intact else None:
            wrong = True
            print(f"{path}: the Queuekeeper rule for {MODES[other]}, but the project is {MODES[mode]}: "
                  "`qk agents setup` rewrites it")
            continue
        print(f"{path}: the Queuekeeper rule, " + ("current" if current else "written by another version, which "
              "`qk agents setup` rewrites" if intact else "edited since it was written"))
    if not rule: print("no agent instructions hold the Queuekeeper rule: `qk agents setup` writes it")
    path = root / SETTINGS
    try: settings = read_settings(path)
    except (OSError, ValueError) as error:
        print(f"{path}: unreadable: {error}")
        return 1
    hooked, broken = {}, False
    for name in (SESSION_START, PRE_TOOL_USE):
        hooked[name] = installed(settings, name)
        print(f"{path}: {EVENTS[name][0]} " + (f"hook `{hooked[name][0]}`" if hooked[name] else "hook absent"))
        for command in hooked[name]:
            if unanchored(command, root):
                broken = True
                print(f"{path}: the {EVENTS[name][0]} hook `{command}` runs its launcher by a path relative to the "
                      "session's directory, so it fails once a session leaves the project root; `qk agents setup` "
                      "anchors it on $CLAUDE_PROJECT_DIR")
    return 0 if rule and hooked[SESSION_START] and not broken and not wrong else 1


def doctor_warnings(root, config):
    """A warning for each agent instructions file holding the other integration's rule, unedited, which tells the
    project's agents to reach main the way it does not (vocaro/queuekeeper#374)."""
    mode = PULL_REQUEST if config.get("integration", DIRECT_MAIN) == PULL_REQUEST else DIRECT_MAIN
    warnings = []
    for name in INSTRUCTIONS:
        try: found, intact = find_block((Path(root) / name).read_text())
        except OSError: continue
        if found is not None and intact and (other := other_mode(found.group(2), RULES[mode])):
            warnings.append(f"{name} tells agents the rule for {MODES[other]}, but queuekeeper.json's integration "
                            f"is {mode}: run `qk agents setup` to write the rule that fits")
    return warnings


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk agents", description="Wire a project's coding agents to the queue: the "
                                     "rule in their instructions and Claude Code's hooks in .claude/settings.json.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name, described in (("setup", "write the rule and merge the hooks; run again to bring them up to date"),
                            ("remove", "take out the rule, unless edited since, and the hooks setup added"),
                            ("check", "say what is in place; exit 0 when the rule and the SessionStart hook are")):
        action = actions.add_parser(name, help=described)
        action.add_argument("--file", action="append", metavar="PATH",
                            help="the agent instructions to change, relative to the project; repeat for more. Without "
                                 "it, setup uses AGENTS.md, else CLAUDE.md, else creates AGENTS.md, and remove and "
                                 "check read both")
        if name == "setup":
            action.add_argument("--guard", action="store_true", help="add the PreToolUse guard, which refuses git push "
                                "to main, git merge on main, moving main with update-ref or branch -f, and gh pr merge")
            action.add_argument("--launcher", default="qk", help="the command that runs Queuekeeper in the hooks "
                                "(default qk; for example python3 scripts/qk). A path in it relative to the project is anchored "
                                "on $CLAUDE_PROJECT_DIR, so that the hooks run wherever the session has gone")
            action.add_argument("--force", action="store_true", help="replace a block edited since it was written")
    args = parser.parse_args(argv)
    if getattr(args, "launcher", "qk") and not shlex.split(getattr(args, "launcher", "qk")):
        parser.error("--launcher names no command")
    root = toplevel(Path(project or ".").resolve())
    if root is None:
        print("queuekeeper: qk agents runs in a Git checkout", file=sys.stderr)
        return 2
    return {"setup": setup, "remove": remove, "check": check}[args.action](root, args)
