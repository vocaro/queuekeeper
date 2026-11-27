"""`qk hook`: the Claude Code hooks `qk agents setup` installs.

Each reads the event's JSON on standard input and answers in Claude Code's hook JSON on standard output. Neither
ever stops a session over its own failure: a hook that cannot read its input, the repository or the queue says
nothing and exits 0.

- session-start tells a starting or resumed session what became of this worktree's and branch's entries since an
  earlier session queued them: sent back, still waiting, or waiting behind a held queue, each with its recovery's
  suggested commands. An agent otherwise learns of a send-back only while it follows `qk land --wait`.
- pre-tool-use refuses a Bash command that would land on main by hand, `git push` to main, `git merge` or `git pull`
  on main other than a fast-forward to the remote's main, moving main with `git update-ref` or `git branch -f`, and
  `gh pr merge`; in a pull-request-queue project, adding the queue's label too (labeling, vocaro/queuekeeper#391).
  It points the agent to `qk land`, or, in a project whose queuekeeper.json says
  `"integration": "pull-request"`, to a pull request that a person merges (vocaro/queuekeeper#374). It is a guard rail
  against a mistake, not a security boundary: it reads the command's words, follows `cd`, `git -C` and `sh -c`, and
  lets through anything it cannot parse."""
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
# How a project's changes reach main: queuekeeper.json's "integration", which config.parse checks.
DIRECT_MAIN, PULL_REQUEST, PULL_REQUEST_QUEUE = "direct-main", "pull-request", "pull-request-queue"


def git(directory, *words):
    """git's output in directory, stripped, or None when it fails."""
    try:
        found = subprocess.run(["git", "-C", str(directory), *words], capture_output=True, text=True, timeout=10,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError): return None
    return found.stdout.strip() if found.returncode == 0 else None


def integration(root):
    """How the project checked out at root integrates, from its queuekeeper.json: pull-request, pull-request-queue
    or direct-main, the default, which a missing or unreadable file gives too. A hook reads it raw, as it must not
    fail over a setting."""
    try: value = json.loads((Path(root) / "queuekeeper.json").read_text()).get("integration", DIRECT_MAIN)
    except (OSError, ValueError, AttributeError): return DIRECT_MAIN
    return value if value in (PULL_REQUEST, PULL_REQUEST_QUEUE) else DIRECT_MAIN


def queue_label(root):
    """The label that approves a pull request for the queue of the project at root, read raw as integration reads its
    integration: pullRequests.label, else queue."""
    try: label = json.loads((Path(root) / "queuekeeper.json").read_text()).get("pullRequests", {}).get("label")
    except (OSError, ValueError, AttributeError): label = None
    return label if isinstance(label, str) and label.strip() else "queue"


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
    newest, when it is waiting, or was sent back recently and is not yet removed. A worktree's folder outlives its
    branch, as Claude Code gives a finished session's folder to a new session on a branch of its own, so an entry
    queued from this folder on another branch is another session's, unless this checkout is on no branch, as in a
    rebase."""
    newest = {}
    for entry in found:
        here = entry.get("directory") and Path(entry["directory"]) == checkout
        mine = here and (not branch or not entry.get("branch") or entry.get("branch") == branch)
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


# Options of git merge and git pull that change only what they print or fetch, never what main becomes.
QUIET_OPTIONS = {"-q", "--quiet", "-v", "--verbose", "--stat", "-n", "--no-stat", "--progress", "--no-progress",
                 "--summary", "--no-summary", "--log", "--no-log"}
PULL_OPTIONS = QUIET_OPTIONS | {"--ff-only", "--no-rebase", "-p", "--prune", "--no-prune", "-t", "--tags",
                                "--no-tags", "--autostash", "--no-autostash"}


def main_remote(directory):
    """The remote main follows, from its upstream configuration or else origin."""
    return git(directory, "config", "--get", f"branch.{MAIN}.remote") or "origin"


def remote_main(directory):
    """The remote-tracking ref of the remote's main, which only the merge queue moves, or None."""
    upstream = git(directory, "rev-parse", "--symbolic-full-name", f"{MAIN}@{{upstream}}") or ""
    if upstream.startswith("refs/remotes/") and upstream.endswith(f"/{MAIN}"): return upstream
    tracking = f"refs/remotes/{main_remote(directory)}/{MAIN}"
    return tracking if git(directory, "rev-parse", "--verify", "-q", tracking) is not None else None


def fast_forwards_to_remote_main(arguments, directory):
    """Whether `git merge ARGUMENTS` on main only fast-forwards it to a commit the remote's main already holds."""
    if "--ff-only" not in arguments: return False
    if any(word.startswith("-") and word not in QUIET_OPTIONS | {"--ff-only"} for word in arguments): return False
    tracking = remote_main(directory)
    if not tracking: return False
    targets = [word for word in arguments if not word.startswith("-")] or [f"{MAIN}@{{upstream}}"]
    if len(targets) > 1: return False
    commit = git(directory, "rev-parse", "--verify", "-q", f"{targets[0]}^{{commit}}")
    return bool(commit) and git(directory, "merge-base", "--is-ancestor", commit, tracking) is not None


def pull_fast_forwards_to_remote_main(arguments, directory):
    """Whether `git pull ARGUMENTS` on main only fast-forwards it to the remote's main."""
    if any(word.startswith("-") and word not in PULL_OPTIONS for word in arguments): return False
    if "--ff-only" not in arguments and git(directory, "config", "--get", "pull.ff") != "only": return False
    if not remote_main(directory): return False
    positional = [word for word in arguments if not word.startswith("-")]
    remote = main_remote(directory)
    if positional and positional[0] != remote: return False
    refspecs = positional[1:]
    if not refspecs:
        merged = git(directory, "config", "--get", f"branch.{MAIN}.merge")
        return merged in (None, f"refs/heads/{MAIN}")
    return all(is_main(spec.split(":", 1)[0]) and ":" not in spec for spec in refspecs)


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
    if action in ("push", "merge", "pull"):
        branch = git(directory, "symbolic-ref", "--short", "-q", "HEAD")
    if action == "push" and push_targets_main(arguments, directory, branch):
        return "git push would update main"
    if action == "merge" and branch == MAIN and not {"--abort", "--continue", "--quit"} & set(arguments) \
            and not fast_forwards_to_remote_main(arguments, directory):
        return "git merge would change main other than by fast-forwarding it to the remote's main"
    if action == "pull" and branch == MAIN and not pull_fast_forwards_to_remote_main(arguments, directory):
        return "git pull would change main other than by fast-forwarding it to the remote's main"
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


# gh's commands that put labels on a pull request or issue as they make or edit it, and the options that name them.
LABELING = {("pr", "edit"): ("--add-label",), ("issue", "edit"): ("--add-label",),
            ("pr", "create"): ("--label", "-l"), ("issue", "create"): ("--label", "-l")}
# A gh api path to an issue's or pull request's labels, and the options that make gh api write rather than read.
LABELS_PATH = re.compile(r"(?:^|/)(?:issues|pulls)/\d+/labels(?:$|[/?])")
WRITES = {"-f", "--raw-field", "-F", "--field", "--input"}


def option_values(arguments, names):
    """Each value arguments give the options names, as `--name VALUE` or `--name=VALUE`."""
    values = []
    for index, word in enumerate(arguments):
        for name in names:
            if word == name and index + 1 < len(arguments): values.append(arguments[index + 1])
            elif word.startswith(name + "=") and name.startswith("--"): values.append(word[len(name) + 1:])
    return values


def labeling(command, label, depth=0):
    """Why command puts label on a pull request, which only a person may do in a pull-request-queue project
    (vocaro/queuekeeper#391), or None: gh pr|issue edit --add-label, gh pr|issue create --label, or a gh api write to an
    issue's or pull request's labels, REST or GraphQL, that names label or reads its fields from a file. Taking the
    label off, which stops a pull request, passes."""
    found = segments(command)
    if found is None or depth > 3: return None
    for words in found:
        words = plain(words)
        if not words: continue
        name = Path(words[0]).name
        if name in SHELLS and "-c" in words[1:-1]:
            inner = labeling(words[words.index("-c", 1) + 1], label, depth + 1)
            if inner: return inner
            continue
        if name != "gh" or len(words) < 2: continue
        options = LABELING.get(tuple(words[1:3]))
        if options and any(label in (part.strip() for part in value.split(","))
                           for value in option_values(words[3:], options)):
            return f"gh {' '.join(words[1:3])} would add the {label} label, a person's approval for the merge queue"
        if words[1] != "api": continue
        arguments = words[2:]
        method = (option_values(arguments, ("-X", "--method")) or ["GET"])[-1].upper()
        writes = method in ("POST", "PUT", "PATCH") or (method == "GET" and any(
            word in WRITES or any(word.startswith(option + "=") for option in WRITES if option.startswith("--"))
            for word in arguments) and not option_values(arguments, ("-X", "--method")))
        if not writes: continue
        named = any(label in word for word in arguments) or "--input" in arguments \
            or any(word.startswith("--input=") for word in arguments)
        if any(LABELS_PATH.search(word) for word in arguments) and named:
            return f"gh api would add the {label} label to a pull request, a person's approval for the merge queue"
        if "graphql" in arguments and any("addLabelsToLabelable" in word for word in arguments):
            return f"gh api graphql would add labels to a pull request, and the {label} label is a person's approval"
    return None


def pre_tool_use(payload):
    if os.environ.get(GUARD_SWITCH, "").strip().lower() == "off": return None
    if payload.get("tool_name") != "Bash": return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str): return None
    directory = Path(payload.get("cwd") or os.getcwd())
    why = refusal(command, directory)
    switch = f"A person can turn this guard off with {GUARD_SWITCH}=off in Claude Code's environment."
    # Only a gh command that may name labels reads the project's mode for them, so that the guard stays quick: one
    # with "label" in it, or -l, gh pr create's short --label.
    if not why and "gh" in command and ("label" in command.lower() or re.search(r"(?:^|\s)-l", command)):
        top = git(directory, "rev-parse", "--show-toplevel")
        if top and integration(top) == PULL_REQUEST_QUEUE:
            label = queue_label(top)
            if labeled := labeling(command, label):
                return (f"Queuekeeper refused this: {labeled}. In this project the {label} label approves a pull "
                        "request for the merge queue, which then merges it, so only a person adds it: open the pull "
                        f"request and leave the label to a person. {switch}")
    if not why: return None
    top = git(directory, "rev-parse", "--show-toplevel")
    mode = integration(top) if top else DIRECT_MAIN
    if mode == PULL_REQUEST:
        return (f"Queuekeeper refused this: {why}, and in this project changes reach main only through a pull request "
                "that a person merges. Commit on your own branch, push it and open a pull request, and leave the "
                f"merge to a person. {switch}")
    if mode == PULL_REQUEST_QUEUE:
        return (f"Queuekeeper refused this: {why}, and in this project changes reach main only through a pull request "
                "that a person labels for the merge queue, which merges it. Commit on your own branch, push it and "
                f"open a pull request, and leave the rest to a person and the queue. {switch}")
    return (f"Queuekeeper refused this: {why}, and changes reach main only through the merge queue. Commit on your own "
            f"branch and run `qk land --wait` as a background command, following it to its result. {switch}")


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
