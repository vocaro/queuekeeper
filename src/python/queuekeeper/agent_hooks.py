"""`qk hook`: the Claude Code hooks `qk agents setup` installs.

Each reads the event's JSON on standard input and answers in Claude Code's hook JSON on standard output. Neither
ever stops a session over its own failure: a hook that cannot read its input, the repository or the queue says
nothing and exits 0.

- session-start tells a starting or resumed session what became of this worktree's and branch's entries since an
  earlier session queued them: sent back, still waiting, or waiting behind a held queue, each with its recovery's
  suggested commands. An agent otherwise learns of a send-back only while it follows `qk land --wait`.
- pre-tool-use refuses a Bash command that would land on main by hand, `git push` to main, `git merge` on main,
  moving main with `git update-ref` or `git branch -f`, and `gh pr merge`, and points the agent to `qk land`. It
  is a guard rail against a mistake, not a security boundary: it reads the command's words, follows `cd`, `git -C`
  and `sh -c`, and lets through anything it cannot parse."""
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time

MAIN = "main"
# A sent-back entry older than this is history, not news for a new session.
RECENT = 14 * 24 * 3600
SHOWN = 10
WHY_LIMIT = 600
SENT_BACK = ("failed", "conflict")
WAITING = ("queued", "testing", "passed")
GUARD_SWITCH = "QUEUEKEEPER_GUARD"


def git(directory, *words):
    """git's output in directory, stripped, or None when it fails."""
    try:
        found = subprocess.run(["git", "-C", str(directory), *words], capture_output=True, text=True, timeout=10,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError): return None
    return found.stdout.strip() if found.returncode == 0 else None


def event():
    try: found = json.loads(sys.stdin.read() or "{}")
    except ValueError: return {}
    return found if isinstance(found, dict) else {}


def answer(name, **fields):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": name, **fields}}))


# SessionStart

def queue_directory(checkout):
    """The repository's queue store, without creating it as merge_queue.Queue would, or None."""
    common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common: return None
    directory = Path(common) / "queuekeeper-queue"
    return directory if (directory / "entries").is_dir() else None


def entries(directory):
    found = []
    for path in (directory / "entries").glob("*.json"):
        try: entry = json.loads(path.read_text())
        except (OSError, ValueError): continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry.get("state"): found.append(entry)
    return sorted(found, key=lambda entry: (entry.get("submitted") or 0, entry["id"]))


def queued_refs(checkout):
    """The IDs whose refs/queuekeeper/queue ref remains: a sent-back entry loses it when its author removes it."""
    listed = git(checkout, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/queuekeeper/queue")
    return set((listed or "").split())


def relevant(found, checkout, branch, refs, now):
    """The entries a session in checkout on branch should hear of: of each branch's or worktree's entries, the
    newest, when it is waiting, or was sent back recently and is not yet removed."""
    newest = {}
    for entry in found:
        mine = entry.get("directory") and Path(entry["directory"]) == checkout
        same_branch = branch and branch not in (MAIN, "HEAD") and entry.get("branch") == branch
        if mine or same_branch: newest[entry.get("branch") or entry.get("directory")] = entry
    chosen = []
    for entry in newest.values():
        if entry["state"] in WAITING: chosen.append(entry)
        elif (entry["state"] in SENT_BACK and entry["id"] in refs
              and now - (entry.get("settled") or entry.get("submitted") or 0) < RECENT): chosen.append(entry)
    return sorted(chosen, key=lambda entry: entry.get("submitted") or 0)[-SHOWN:]


def hold(directory):
    """The queue's hold as (why, recovery), or (None, None)."""
    from . import recovery
    path = directory / "hold"
    try: why = path.read_text().strip()
    except OSError: return None, None
    return (why, recovery.read_hold(path, why)) if why else (None, None)


def notice(chosen, checkout, held, held_recovery):
    from . import recovery
    lines = ["Queuekeeper: entries this worktree or branch put in the merge queue need attention."]
    for entry in chosen:
        state = entry["state"]
        advice = recovery.in_project(recovery.for_entry(entry, held_recovery), checkout)
        what = {"failed": "came back: its gate failed", "conflict": "came back: it conflicts",
                }.get(state, f"is still in the queue ({state})" + (", which is held" if held else ""))
        line = f"- {entry['id']} ({entry.get('branch') or 'no branch'}: {entry.get('subject') or ''}) {what}."
        why = (entry.get("why") or "").strip() if state in SENT_BACK else (held or "")
        if why: line += " " + (why if len(why) <= WHY_LIMIT else why[:WHY_LIMIT] + "...")
        if advice.get("suggestedCommands"):
            line += " Next: " + "; ".join(f"`{shlex.join(command)}`" for command in advice["suggestedCommands"])
        lines.append(line)
    if any(entry["state"] in SENT_BACK for entry in chosen):
        lines.append("Fix what came back on its branch and land it again with `qk land --wait`, or take it out with "
                     "`qk queue remove ID` if it is no longer wanted.")
    return "\n".join(lines)


def session_start(payload):
    checkout = Path(payload.get("cwd") or os.getcwd())
    top = git(checkout, "rev-parse", "--show-toplevel")
    if not top: return None
    checkout = Path(top).resolve()
    directory = queue_directory(checkout)
    if directory is None: return None
    branch = git(checkout, "symbolic-ref", "--short", "-q", "HEAD")
    chosen = relevant(entries(directory), checkout, branch, queued_refs(checkout), time.time())
    if not chosen: return None
    held, held_recovery = hold(directory)
    return notice(chosen, checkout, held, held_recovery)


# PreToolUse

SHELLS = {"sh", "bash", "zsh", "dash"}
WRAPPERS = {"sudo", "command", "exec", "nohup", "time", "builtin"}
# git's options before its command that take a value as the next word.
GIT_VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def segments(command):
    """command's simple commands, each a list of words with their quotes taken off, or None when a quote is left open.
    ;, &, |, parentheses and a newline outside quotes end a command; # at a word's start begins a comment."""
    found, current, word, quote, index = [], [], None, None, 0
    def finish_word():
        nonlocal word
        if word is not None: current.append(word)
        word = None
    def finish_command():
        nonlocal current
        finish_word()
        if current: found.append(current)
        current = []
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'": quote = None
            else: word += character
        elif quote == '"':
            if character == '"': quote = None
            elif character == "\\" and command[index + 1:index + 2] in ('"', "\\", "$", "`"):
                index += 1
                word += command[index]
            else: word += character
        elif character == "\\":
            index += 1
            if command[index:index + 1] != "\n": word = (word or "") + command[index:index + 1]
        elif character in "'\"":
            quote = character
            word = word or ""
        elif character == "&" and word and word[-1] in "<>": word += character
        elif character in ";&|()\n": finish_command()
        elif character in " \t\r": finish_word()
        elif character == "#" and word is None:
            while index < len(command) and command[index] != "\n": index += 1
            continue
        else: word = (word or "") + character
        index += 1
    if quote: return None
    finish_command()
    return found


REDIRECTION = re.compile(r"\d*(?:[<>]+&?|&>>?)(.*)")


def plain(words):
    """words without their redirections, and without leading variable assignments, env and wrappers such as sudo."""
    unredirected, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        redirected = REDIRECTION.fullmatch(word)
        if redirected: skip = not redirected.group(1)
        else: unredirected.append(word)
    words = unredirected
    while words:
        if "=" in words[0] and not words[0].startswith("-") and words[0].split("=", 1)[0].isidentifier():
            words.pop(0)
        elif words[0] in WRAPPERS or Path(words[0]).name == "env":
            words.pop(0)
            while words and words[0].startswith("-"): words.pop(0)
        else: break
    return words


def is_main(ref):
    ref = ref.lstrip("+")
    return ref in (MAIN, f"refs/heads/{MAIN}")


# git push's options that take the next word as their value.
PUSH_VALUED = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}


def push_targets_main(arguments, directory, branch):
    """Whether `git push ARGUMENTS`, from directory's checkout on branch, updates main."""
    positional, skip = [], False
    for word in arguments:
        if skip: skip = False
        elif word in ("--all", "--mirror", "--branches"): return True
        elif word in PUSH_VALUED: skip = True
        elif not word.startswith("-"): positional.append(word)
    refspecs = positional[1:]
    if not refspecs:
        # Where a bare push goes: the current branch's push destination, which upstream tracking can make main.
        if branch == MAIN: return True
        pushed = git(directory, "rev-parse", "--symbolic-full-name", "@{push}") or ""
        return pushed.rsplit("/", 1)[-1] == MAIN and pushed.startswith("refs/remotes/")
    for spec in refspecs:
        destination = spec.rsplit(":", 1)[1] if ":" in spec else spec
        if destination.lstrip("+") in ("HEAD", "@"): destination = branch or ""
        if is_main(destination): return True
    return False


def git_refusal(words, directory):
    """Why the git command words, run in directory, lands on main by hand, or None."""
    index = 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option in GIT_VALUED and index + 1 < len(words):
            if option == "-C": directory = directory / words[index + 1]
            index += 2
        else:
            if option.startswith("-C") and len(option) > 2: directory = directory / option[2:]
            index += 1
    if index >= len(words): return None
    action, arguments = words[index], words[index + 1:]
    branch = None
    if action in ("push", "merge"):
        branch = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    if action == "push" and push_targets_main(arguments, directory, branch):
        return "git push would update main"
    if action == "merge" and branch == MAIN and not {"--abort", "--continue", "--quit"} & set(arguments):
        return "git merge would change main"
    if action == "update-ref" and any(is_main(word) for word in arguments if not word.startswith("-")):
        return "git update-ref would move main"
    if action == "branch" and any(word in ("-f", "--force", "-D", "-M", "-C") or word.startswith("--force")
                                  for word in arguments) and any(is_main(word) for word in arguments):
        return "git branch would move or delete main"
    return None


def refusal(command, directory, depth=0):
    """Why command lands on main by hand, or None."""
    found = segments(command)
    if found is None or depth > 3: return None
    for words in found:
        words = plain(words)
        if not words: continue
        name = Path(words[0]).name
        if name == "cd":
            target = next((word for word in words[1:] if not word.startswith("-")), None)
            directory = (directory / os.path.expanduser(target)) if target and target != "-" else Path.home()
            continue
        if name in SHELLS and "-c" in words[1:-1]:
            inner = refusal(words[words.index("-c", 1) + 1], directory, depth + 1)
            if inner: return inner
            continue
        if name == "git":
            why = git_refusal(words, directory)
            if why: return why
        if name == "gh" and words[1:3] == ["pr", "merge"]:
            return "gh pr merge would merge a pull request into main"
    return None


def pre_tool_use(payload):
    if os.environ.get(GUARD_SWITCH, "").strip().lower() == "off": return None
    if payload.get("tool_name") != "Bash": return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str): return None
    why = refusal(command, Path(payload.get("cwd") or os.getcwd()))
    if not why: return None
    return (f"Queuekeeper refused this: {why}, and changes reach main only through the merge queue. Commit on your own "
            "branch and run `qk land --wait` as a background command, following it to its result. A person can turn "
            f"this guard off with {GUARD_SWITCH}=off in Claude Code's environment.")


def main(argv, project=None):
    name = argv[0] if argv else None
    if name not in ("session-start", "pre-tool-use") or len(argv) > 1:
        print("usage: qk hook {session-start,pre-tool-use} < the event's JSON\n"
              "Claude Code's hooks, which qk agents setup installs.", file=sys.stderr)
        return 2
    try:
        payload = event()
        if name == "session-start":
            context = session_start(payload)
            if context: answer("SessionStart", additionalContext=context)
        else:
            why = pre_tool_use(payload)
            if why: answer("PreToolUse", permissionDecision="deny", permissionDecisionReason=why)
    except Exception as error:  # noqa: BLE001 -- a hook's own failure never stops a session
        print(f"queuekeeper: hook {name}: {error}", file=sys.stderr)
    return 0
