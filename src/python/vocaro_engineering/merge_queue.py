"""A local merge queue for a direct-main repository: `eng queue add`, and a session moves on.

One worker per repository takes everything waiting, cherry-picks it onto origin/main in a checkout
of its own, runs the project's queue lanes there once, and pushes on a pass. When the lanes fail on
a batch it halves the batch until it finds the commit to send back, landing the rest. Only a lane's
verdict blames a commit: a run the host left unmeasured retries once and then holds the queue for
the owner, with every commit still waiting (guides/maintenance.md, The merge queue).

The state is in the repository's common Git directory, so every worktree shares it, and a ref holds
each waiting commit, so a branch that moves or goes takes nothing with it:

    <common>/eng-queue/entries/ID.json   one commit range and its state
    <common>/eng-queue/logs/             what each round's lanes printed
    <common>/eng-queue/hold              why the queue stopped, until `eng queue resume`
    refs/eng-queue/ID                    the range's tip, until it lands or is removed
    ../NAME-queue                        the worker's checkout, beside the main checkout
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from . import evidence, landing, processes, remote, resources
from .config import TOOLKIT_ROOT, load, parse

MAIN = "refs/remotes/origin/main"
FETCH = ["fetch", "--no-tags", "origin", f"refs/heads/main:{MAIN}"]
QUEUED, TESTING, LANDED, FAILED, CONFLICT, REMOVED = "queued", "testing", "landed", "failed", "conflict", "removed"
WAITING = {QUEUED, TESTING}
# What a lane exits with when it says nothing about the commits: unmeasured, no admission, and a
# shared host's configuration, which stops for the owner.
NO_VERDICT = {evidence.UNMEASURED, resources.BUSY, remote.MISCONFIGURED}
# Seconds a whole lane may take with its wait for admission: a lane's own limit is 2 h by default
# (processes.DEFAULT_LIMIT), and this allows another two of them in the host's queue.
LANE_LIMIT = 3 * processes.DEFAULT_LIMIT
# A run with no verdict is tried again once, since a busy host clears, before the queue holds.
RETRIES = 1
# Times main may move past a passed batch, in carried-over steps, before the batch is gated again.
CARRIES = 3
# What git says when the connection to the remote ended, which says nothing of the commits.
DROPPED = re.compile(r"hung up unexpectedly|Could not read from remote|send disconnect|Connection (reset|closed|timed out)")
POLL = 5
# What a project's launcher reads first to find the toolkit.
TOOLKIT_VARIABLE = "VOCARO_ENGINEERING_ROOT"
BEFORE_GATE = ["spelling", "provision"]
WAIT_LIMIT = 6 * 3600
KEPT_LOGS = 200
GIT_TIMEOUT = 600
# The file whose conflicts the queue may settle, by its name in any directory and in any case.
CHANGELOG = "changelog.md"
HEADING = re.compile(rb"#{1,6}\s")
# Long enough that no line of a changelog reads as one of `git merge-file`'s markers.
MARKER_SIZE = 32


def environment():
    """The environment without what a hook or an admission left in it: a pushing session's GIT_DIR
    would aim the worker's Git at its own checkout, and an inherited admission token would run the
    lanes as nested work whose owner has gone."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
    drop = set(names.stdout.split()) | {resources.TOKEN}
    return {key: value for key, value in os.environ.items() if key not in drop}


def git(directory, *arguments, hooks=True, check=False, timeout=GIT_TIMEOUT):
    """Git in directory. Without hooks, for what the worker does in its own checkout: a project's
    checkout hook may open an editor on the owner's screen (guides/maintenance.md)."""
    command = ["git", *([] if hooks else ["-c", "core.hooksPath=/dev/null"]), "-C", str(directory), *arguments]
    done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=environment(),
                          timeout=timeout)
    if check and done.returncode:
        raise ValueError(f"`git {shlex.join(arguments)}` exited {done.returncode}: "
                         f"{done.stderr.strip() or 'no output'}")
    return done


def text(directory, *arguments):
    return git(directory, *arguments, check=True).stdout.strip()


def lanes_of(config):
    """The lanes a batch must pass, in order: the project's spelling and provision lanes when it has
    them, the cheap checks and setup that a gate assumes, and then `queue.lanes` or its gate lane."""
    lanes = config.get("lanes", {})
    named = config.get("queue", {}).get("lanes") or (["gate"] if "gate" in lanes else None)
    if not named:
        raise ValueError(f"{config.get('name')} has no gate lane, so the queue has nothing to run: name the lane "
                         "holding what a commit must pass before it lands gate (guides/maintenance.md, "
                         "The merge queue)")
    return [name for name in BEFORE_GATE if name in lanes and name not in named] + named


def config_at(root, commit):
    """The project's configuration as commit has it, not as a checkout's working tree does, which
    may lag: the queue serves every session from one main checkout that nobody keeps current."""
    shown = git(root, "show", f"{commit}:engineering.json")
    if shown.returncode: raise ValueError(f"{commit} has no engineering.json: {shown.stderr.strip() or 'no output'}")
    return parse(shown.stdout)


def blank(line):
    return not line.strip()


def both_added(before, ours, base, theirs, held):
    """What one conflict block becomes, or None unless each side only adds whole lines at the top of a
    section, with none of the commit's lines already in the file: the new lines, then those ahead of them.
    A blank line both sides add around their lines stays where it is; any other blank line, or a heading,
    is no longer a list of entries, and keeping both could put them under the wrong section."""
    if base or not ours or not theirs or not all(line.endswith(b"\n") for line in ours + theirs): return None
    start = end = 0
    shortest = min(len(ours), len(theirs))
    while start < shortest and blank(ours[start]) and blank(theirs[start]): start += 1
    while end < shortest - start and blank(ours[-1 - end]) and blank(theirs[-1 - end]): end += 1
    new, old = theirs[start:len(theirs) - end], ours[start:len(ours) - end]
    if not new or not old or any(blank(line) or HEADING.match(line) for line in new + old): return None
    above = next((line for line in reversed(before + ours[:start]) if not blank(line)), b"")
    if not HEADING.match(above): return None
    stripped = [line.strip() for line in new]
    if len(set(stripped)) != len(stripped) or set(stripped) & held: return None
    return ours[:start] + new + old + ours[len(ours) - end:]


def keep_both(base, ours, theirs):
    """The file, as bytes, with each of its conflicts settled by both_added, or None when it has none or
    both_added refuses any: theirs is the commit being applied, and ours what is ahead of it."""
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, content in zip(paths, (ours, base, theirs)): path.write_bytes(content)
        merged = subprocess.run(["git", "merge-file", "-p", "--diff3", f"--marker-size={MARKER_SIZE}", *map(str, paths)],
                                capture_output=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT)
    # Its status is how many conflicts it left, and above 127 an error.
    if not 0 < merged.returncode <= 127: return None
    open_, middle, split, close = (mark * MARKER_SIZE for mark in (b"<", b"|", b"=", b">"))
    held = {line.strip() for line in ours.splitlines()}
    kept, block = [], None
    for line in merged.stdout.splitlines(keepends=True):
        if line.startswith(open_): block = current = [[], [], []]; side = 0
        elif block is None: kept.append(line)
        elif line.startswith(middle): side = 1
        elif line.startswith(split): side = 2
        elif line.startswith(close):
            settled = both_added(kept, current[0], current[1], current[2], held)
            if settled is None: return None
            kept += settled
            block = None
        else: current[side].append(line)
    return b"".join(kept)


def named(path, key):
    """Whether a queue setting's key names path: a directory ending in /, or the file itself."""
    return path.startswith(key) if key.endswith("/") else path == key


def carried_over(path, config, batch_files):
    """Whether main changing path leaves a passed batch's result standing: a path the project lists
    in queue.carryOver as read by none of its queue lanes, or a Markdown document, and in either
    case one the batch does not touch."""
    if path in batch_files: return False
    listed = config.get("queue", {}).get("carryOver", {})
    return path.endswith(".md") or any(named(path, key) for key in listed)


def carry_checks(config, moved, base):
    """The commands a carry-over over moved must pass on the rebased tree: `eng docs check` when a document
    moved, then each of queue.carryOverChecks whose paths include a moved file, or that has none. {base} in
    a command is the commit the result passed on, so a check that compares with origin/main, which the
    batch now sits on, can read what main's new commits added."""
    commands = [["python3", "scripts/eng", "docs", "check"]] if any(path.endswith(".md") for path in moved) else []
    for check in config.get("queue", {}).get("carryOverChecks", []):
        if "paths" not in check or any(named(path, key) for path in moved for key in check["paths"]):
            commands.append([word.replace("{base}", base) for word in check["command"]])
    return commands


class Queue:
    def __init__(self, root):
        self.root = Path(root)
        common = Path(text(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.main_checkout = common.parent
        self.directory = common / "eng-queue"
        self.entries = self.directory / "entries"
        self.logs = self.directory / "logs"
        self.hold_file = self.directory / "hold"
        self.checkout = self.main_checkout.parent / f"{self.main_checkout.name}-queue"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def locked(self):
        """Every read-then-write of the entries, between sessions and the worker."""
        with (self.directory / "state.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, entry):
        path = self.entries / f"{entry['id']}.json"
        staged = path.with_suffix(f".{os.getpid()}.tmp")
        staged.write_text(json.dumps(entry, indent=2) + "\n")
        os.replace(staged, path)

    def read(self, identifier):
        try: return json.loads((self.entries / f"{identifier}.json").read_text())
        except (OSError, ValueError): return None

    def all(self):
        """Every entry, oldest first."""
        found = []
        for path in self.entries.glob("*.json"):
            with contextlib.suppress(OSError, ValueError): found.append(json.loads(path.read_text()))
        return sorted(found, key=lambda entry: (entry["submitted"], entry["id"]))

    def waiting(self):
        return [entry for entry in self.all() if entry["state"] in WAITING]

    def settle(self, entry, state, **fields):
        """A final state. The ref goes with a landed or removed range; a failed one keeps it until
        its author removes the entry, since its branch may be gone."""
        with self.locked():
            current = self.read(entry["id"]) or entry
            if current["state"] not in WAITING: return current
            current.update(state=state, settled=time.time(), **fields)
            self.save(current)
        if state in (LANDED, REMOVED): git(self.root, "update-ref", "-d", f"refs/eng-queue/{entry['id']}")
        return current

    def mark(self, entries, state):
        """Queued or testing, for the entries nobody has removed meanwhile."""
        with self.locked():
            for entry in entries:
                current = self.read(entry["id"])
                if current and current["state"] in WAITING:
                    current["state"] = state
                    self.save(current)

    def held(self):
        try: return self.hold_file.read_text().strip()
        except OSError: return None

    def hold(self, why):
        self.hold_file.write_text(why + "\n")
        print(f"queue: held: {why}", flush=True)

    @contextlib.contextmanager
    def worker(self):
        """The repository's one worker, or None while another process is it."""
        with (self.directory / "worker.lock").open("w") as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True

    def working(self):
        with self.worker() as free: return not free

    def start(self):
        """Start a worker apart from this session, so that it outlives the session's turn and its
        task stop. One that finds another working exits at once."""
        if self.held() or not self.waiting() or self.working(): return
        source = str(TOOLKIT_ROOT / "src/python")
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from vocaro_engineering.cli import main; "
                  "sys.exit(main(['--project', sys.argv[2], 'queue', 'run']))")
        with (self.directory / "worker.log").open("a") as log:
            started = subprocess.Popen([sys.executable, "-c", script, source, str(self.main_checkout)], cwd=self.main_checkout,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=environment(),
                             start_new_session=True)
            # It is meant to outlive this process, which must not wait for it, but one that ends first
            # stays a zombie child until reaped: a long-lived caller, `eng land --wait` or a test run, sees it.
            threading.Thread(target=started.wait, daemon=True).start()


def repository(root, named):
    """The repository `eng land --repo` names: a path, or the name of a project beside this one's
    main checkout, as a session fixing a dependency names it."""
    candidates = [Path(named).expanduser(), Queue(root).main_checkout.parent / named]
    for path in candidates:
        if (path / "engineering.json").is_file() and git(path, "rev-parse", "--git-dir").returncode == 0:
            return path.resolve()
    raise ValueError(f"--repo {named} names no project: give a checkout's path, or the name of a project beside "
                     f"{Queue(root).main_checkout}")


def land(root, args):
    """`eng land`: add to this repository's queue or, with --repo, another's, whose own queue, lanes and
    worker take it exactly as they take that repository's own sessions' commits. With another
    repository's main checkout HEAD would be whatever it has checked out, so the commit is named."""
    target = root
    if args.repo:
        target = repository(root, args.repo)
        if args.reference is None:
            raise ValueError(f"--repo lands a commit you name: `eng land --repo {args.repo} BRANCH`, since HEAD "
                             "there is another session's")
    identifier = add(target, args.reference or "HEAD", args.note, quiet_wait=args.wait)
    if not args.wait or identifier is None: return 0
    return wait(target, identifier, args.limit)


def add(root, reference, note=None, quiet_wait=False):
    """Put the commits reference has and origin/main lacks in the queue, and return at once with the
    entry's ID, or None when there is nothing to queue. Whether it may be queued is what a round would
    find: origin/main's settings, and origin/main's lanes or, until it has any, the commit's."""
    queue = Queue(root)
    git(root, *FETCH, check=True)
    tip = text(root, "rev-parse", "--verify", f"{reference}^{{commit}}")
    config = config_at(root, MAIN)
    if config.get("integration", "direct-main") != "direct-main":
        raise ValueError(f"{config.get('name')} integrates by pull request, which the queue does not push to")
    try: lanes_of(config)
    except ValueError: lanes_of(config_at(root, tip))
    if git(root, "merge-base", "--is-ancestor", tip, MAIN).returncode == 0:
        print(f"origin/main already holds {tip[:12]}: nothing to queue")
        return None
    base = text(root, "merge-base", tip, MAIN)
    commits = text(root, "rev-list", "--reverse", f"{base}..{tip}").split()
    merges = text(root, "rev-list", "--merges", f"{base}..{tip}").split()
    if merges:
        raise ValueError(f"{merges[0][:12]} is a merge, which a queue would have to put on main: rebase the branch's "
                         "own commits onto origin/main and add it again")
    if reference == "HEAD" and git(root, "status", "--porcelain").stdout.strip():
        print("queue: the checkout has uncommitted changes, which stay out: only commits are queued", file=sys.stderr)
    branch = git(root, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() if reference == "HEAD" else \
        git(root, "rev-parse", "--abbrev-ref", reference).stdout.strip()
    with queue.locked():
        waiting = [entry for entry in queue.all() if entry["state"] in WAITING]
        same = next((entry for entry in waiting if entry["tip"] == tip), None)
    if same:
        print(f"{same['id']} already holds {tip[:12]} ({same['state']})")
        queue.start()
        return same["id"]
    # A branch added again replaces what it added before; main names nobody's work in particular.
    if branch and branch not in ("main", "HEAD"):
        for entry in waiting:
            if entry.get("branch") == branch:
                remove(queue, entry["id"], quiet=True)
                print(f"queue: replaced {entry['id']}, the branch's earlier commits", file=sys.stderr)
    now = time.time()
    identifier = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{tip[:8]}"
    entry = {"id": identifier, "tip": tip, "base": base, "commits": commits, "branch": branch or None, "note": note,
             "subject": text(root, "log", "-1", "--format=%s", tip), "submitted": now, "state": QUEUED,
             "directory": str(root)}
    text(root, "update-ref", f"refs/eng-queue/{identifier}", tip)
    with queue.locked(): queue.save(entry)
    print(f"queued {identifier}: {len(commits)} commit(s), {entry['subject']}")
    why = queue.held()
    if why: print(f"queue: held, so it waits until the owner runs `eng queue resume`: {why}", file=sys.stderr)
    queue.start()
    if not quiet_wait:
        print("nothing else is needed for it to land; to follow it to its result: "
              f"python3 {shlex.quote(str(root))}/scripts/eng queue wait {identifier}")
    return identifier


def remove(queue, identifier, quiet=False):
    entry = queue.read(identifier)
    if entry is None: raise ValueError(f"The queue has no entry {identifier}")
    if entry["state"] in WAITING: queue.settle(entry, REMOVED)
    else: git(queue.root, "update-ref", "-d", f"refs/eng-queue/{identifier}")
    if not quiet: print(f"removed {identifier}")
    return 0


def describe(entry):
    line = f"{entry['state']:8} {entry['id']}  {entry['subject']}"
    if entry.get("landedAs"): line += f"  -> {entry['landedAs'][:12]}"
    if entry.get("why"): line += f"\n         {entry['why']}"
    if entry.get("log"): line += f"\n         log: {entry['log']}"
    return line


def status(root, as_json=False, recent=5):
    queue = Queue(root)
    entries = queue.all()
    waiting = [entry for entry in entries if entry["state"] in WAITING]
    done = sorted((entry for entry in entries if entry["state"] not in WAITING),
                  key=lambda entry: entry.get("settled", 0))[-recent:]
    working, why = queue.working(), queue.held()
    if as_json:
        print(json.dumps({"worker": working, "held": why, "waiting": waiting, "recent": done}, indent=2))
        return 0
    print(f"worker: {'running' if working else 'none'}" + (f"; held: {why}" if why else ""))
    if waiting and not working and not why: print("waiting with no worker: `eng queue resume` starts one")
    for entry in waiting + done: print(describe(entry))
    return 0


def wait(root, identifier, limit=WAIT_LIMIT):
    """Exit 0 once the entry lands, 1 when it comes back to its author, 75 while the queue is held or
    the limit passes with the entry still waiting. A worker a reboot ended starts again here."""
    queue = Queue(root)
    deadline = time.monotonic() + limit
    while True:
        entry = queue.read(identifier)
        if entry is None: raise ValueError(f"The queue has no entry {identifier}")
        if entry["state"] not in WAITING:
            print(describe(entry))
            # In full, for the pin a dependent project moves to.
            if entry["state"] == LANDED: print(f"landed: {entry['landedAs']}")
            return 0 if entry["state"] == LANDED else 1
        why = queue.held()
        if why:
            print(f"{identifier} is waiting, and the queue is held for the owner: {why}")
            return resources.BUSY
        if time.monotonic() >= deadline:
            print(f"{identifier} is still {entry['state']} after {limit:g}s")
            return resources.BUSY
        queue.start()
        time.sleep(POLL)


class Worker:
    def __init__(self, queue):
        self.queue = queue
        self.checkout = queue.checkout
        self.limit = None  # how many of the oldest entries the next round takes, while bisecting
        self.suspects = 0  # how many of the oldest entries hold a commit that fails
        self.retries = 0
        self.environment = environment()  # what the lanes, the hook and the carry-over checks run in

    def say(self, words):
        print(f"queue: {time.strftime('%H:%M:%S')} {words}", flush=True)

    def prepare(self):
        """The worker's own checkout. Locked, so that no sweep takes it for an abandoned worktree."""
        listed = text(self.queue.main_checkout, "worktree", "list", "--porcelain")
        registered = f"worktree {self.checkout}" in listed.splitlines()
        if not registered:
            if self.checkout.exists():
                raise ValueError(f"{self.checkout} exists and is no worktree of this repository: move it, then run "
                                 "`eng queue resume`")
            git(self.queue.main_checkout, "worktree", "add", "--detach", "--lock", "--reason",
                "the merge queue's checkout", str(self.checkout), MAIN, hooks=False, check=True)

    def toolkit(self):
        """Run this round's lanes on the toolkit at origin/main, in a copy no session edits: a shared
        checkout that lags, or holds a session's changes, says nothing of the commits (guides/maintenance.md,
        The merge queue). Each worker has its own, so that moving it changes no other project's running
        gate. The toolkit's own queue tests its batch, and an explicit VOCARO_ENGINEERING_ROOT stands.
        A copy that cannot be made or moved holds the queue, as a fetch of the project that fails does."""
        self.environment = environment()
        if os.environ.get(TOOLKIT_VARIABLE): return
        common = git(TOOLKIT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode: return  # a toolkit that is no repository has no origin/main to follow
        if common.stdout.strip() == text(self.queue.main_checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            return
        git(TOOLKIT_ROOT, *FETCH, check=True)
        copy = self.checkout.with_name(f"{self.checkout.name}-toolkit")
        if f"worktree {copy}" not in text(TOOLKIT_ROOT, "worktree", "list", "--porcelain").splitlines():
            if copy.exists():
                raise ValueError(f"{copy} exists and is no worktree of the toolkit: move it")
            git(TOOLKIT_ROOT, "worktree", "add", "--detach", "--lock", "--reason", "the merge queue's copy of the toolkit",
                str(copy), MAIN, hooks=False, check=True)
        git(copy, "checkout", "--force", "--detach", text(TOOLKIT_ROOT, "rev-parse", MAIN), hooks=False, check=True)
        git(copy, "clean", "-fdq", hooks=False, check=True)
        self.say(f"toolkit {text(copy, 'rev-parse', 'HEAD')[:12]} in {copy}")
        self.environment = {**environment(), TOOLKIT_VARIABLE: str(copy)}

    def reset(self, commit):
        """Exactly commit's tracked files. Ignored inputs a lane provisioned stay for the next round."""
        git(self.checkout, "cherry-pick", "--abort", hooks=False)
        git(self.checkout, "checkout", "--force", "--detach", commit, hooks=False, check=True)
        git(self.checkout, "clean", "-fdq", hooks=False, check=True)

    def head(self):
        return text(self.checkout, "rev-parse", "HEAD")

    def pick(self, entry):
        """Cherry-pick entry's commits onto HEAD. None when they apply, else why, with HEAD as it was."""
        before = self.head()
        for commit in entry["commits"]:
            picked = git(self.checkout, "cherry-pick", commit, hooks=False)
            if picked.returncode == 0: continue
            conflicted = text(self.checkout, "diff", "--name-only", "--diff-filter=U").split()
            if not conflicted and not git(self.checkout, "status", "--porcelain").stdout.strip():
                # main already has this commit's change, as another route may have landed it.
                git(self.checkout, "cherry-pick", "--skip", hooks=False, check=True)
                continue
            if conflicted and self.keep_changelogs(conflicted):
                # -C, for the commit's own message and author, without Git's note of the conflict.
                git(self.checkout, "commit", "--no-verify", "-C", commit, hooks=False, check=True)
                self.say(f"{commit[:12]}: kept both sides of {', '.join(conflicted)}, the new lines first")
                continue
            self.reset(before)
            files = ", ".join(conflicted) or picked.stderr.strip()
            return f"{commit[:12]} conflicts with what is ahead of it, in {files}: rebase onto origin/main and add it again"
        return None

    def blob(self, identifier):
        return subprocess.run(["git", "-C", str(self.checkout), "cat-file", "blob", identifier], capture_output=True,
                              check=True, stdin=subprocess.DEVNULL, env=environment(), timeout=GIT_TIMEOUT).stdout

    def keep_changelogs(self, conflicted):
        """Settle the cherry-pick in progress when every file in conflict is a changelog whose conflicts
        keep_both settles, and say whether it did; otherwise change nothing."""
        settled = {}
        for path in conflicted:
            if Path(path).name.lower() != CHANGELOG: return False
            stages = {}
            for line in text(self.checkout, "ls-files", "-u", "--", path).splitlines():
                mode, identifier, stage = line.split("\t")[0].split()
                stages[stage] = (mode, identifier)
            # Three stages of one ordinary file: a rename, a deletion or a change of mode is no added line.
            if set(stages) != {"1", "2", "3"} or len({mode for mode, _ in stages.values()}) != 1 \
                    or not stages["1"][0].startswith("100"):
                return False
            settled[path] = keep_both(*(self.blob(stages[stage][1]) for stage in "123"))
            if settled[path] is None: return False
        for path, content in settled.items():
            (self.checkout / path).write_bytes(content)
            git(self.checkout, "add", "--", path, hooks=False, check=True)
        return True

    def integrate(self, batch, base):
        """The entries of batch that are now on top of base, in order; the rest are settled."""
        self.reset(base)
        applied = []
        for entry in batch:
            how = landing.contained(lambda *words: git(self.checkout, *words), base, entry["tip"])
            if how:
                self.queue.settle(entry, LANDED, landedAs=base, why=f"origin/main already holds it ({how})")
                continue
            why = self.pick(entry)
            if why:
                self.say(f"{entry['id']}: {why}")
                self.queue.settle(entry, CONFLICT, why=why)
                continue
            applied.append(entry)
        return applied

    def gate(self, config, round_name):
        """(status of the first lane that did not pass or 0, its log)."""
        log = None
        for lane in lanes_of(config):
            log = self.queue.logs / f"{round_name}-{lane}.log"
            # A lane run here logs to its own file, and one on the second Mac prints here. What an
            # earlier round left there is not this one's.
            lane_log = self.checkout / ".build/engineering" / lane / "run.log"
            with contextlib.suppress(OSError): lane_log.unlink()
            self.say(f"{lane} on {self.head()[:12]}")
            try:
                status_ = processes.run(["python3", "scripts/eng", "run", lane], cwd=self.checkout, log_path=log,
                                        timeout=LANE_LIMIT, env=self.environment)
            except processes.TimedOut as error:
                self.say(f"{lane}: unmeasured: {error}")
                return evidence.UNMEASURED, log
            with contextlib.suppress(OSError), log.open("a") as kept: kept.write(lane_log.read_text(errors="replace"))
            self.say(f"{lane} exited {status_}")
            if status_: return status_, log
        return 0, log

    def changed(self, old, new):
        return text(self.checkout, "diff", "--name-only", old, new).split("\n")

    def carry_passes(self, commands):
        """Whether every command exits 0 in the checkout, each bounded and with a log of its own. One that
        fails, times out or cannot start says nothing of the commits: it only ends the carry-over."""
        for number, command in enumerate(commands):
            log = self.queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.head()[:8]}-carry-{number}.log"
            self.say(f"carry-over check: {shlex.join(command)}")
            try: status_ = processes.run(command, cwd=self.checkout, log_path=log, timeout=LANE_LIMIT, env=self.environment)
            except OSError as error: status_ = error
            if status_:
                self.say(f"carry-over check {shlex.join(command)} ended with {status_}: gating again; see {log}")
                return False
        return True

    def carry(self, config, batch, base, head):
        """(new base, new head) with the batch on origin/main, when main moved past base only by files the
        lanes do not read and the batch does not touch, else None: guides/maintenance.md, Push only onto
        the main you tested. The patch must be the one that passed, and the documents and the project's
        carry-over checks must pass on the rebased tree."""
        new_base = text(self.checkout, "rev-parse", MAIN)
        batch_files = set(self.changed(base, head))
        moved = [path for path in self.changed(base, new_base) if path]
        refused = [path for path in moved if not carried_over(path, config, batch_files)]
        if refused:
            self.say(f"main moved by {refused[0]}, which the result does not cover: gating again")
            return None
        patch = text(self.checkout, "diff", base, head)
        self.reset(new_base)
        if any(self.pick(entry) for entry in batch):
            self.say("the batch no longer applies to main: gating again")
            return None
        if text(self.checkout, "diff", new_base, "HEAD") != patch:
            self.say("the batch's patch changed on main: gating again")
            return None
        if not self.carry_passes(carry_checks(config, moved, base)): return None
        self.say(f"carried the result over {len(moved)} file(s) on main: {', '.join(moved[:5])}")
        return new_base, self.head()

    def push(self, config, batch, base):
        """Push HEAD to main. True on a push, False to gate again; a refusal that is no race holds.
        The project's pre-push hook runs here rather than inside the push, unless main's queue.hookCoveredBy
        says which passed lanes cover it: a long hook then holds no connection open, and its refusal is its own."""
        head = self.head()
        dropped = unsure = False
        checked = None
        covered = config.get("queue", {}).get("hookCoveredBy")
        for _ in range(CARRIES):
            git(self.checkout, *FETCH, check=True)
            if git(self.checkout, "merge-base", "--is-ancestor", MAIN, head).returncode:
                carried = self.carry(config, batch, base, head)
                if carried is None: return False
                base, head = carried
            if covered: self.say(f"no pre-push hook: main's queue.hookCoveredBy says {', '.join(covered)} cover it")
            elif checked != head:
                result, log = self.pre_push(head)
                if result in NO_VERDICT and not unsure:
                    unsure = True
                    self.say(f"the pre-push hook gave no verdict (exit {result}): trying it again")
                    continue
                if result in NO_VERDICT:
                    raise ValueError(f"the pre-push hook gave no verdict, exit {result}, twice on {head[:12]}; see {log}")
                if result: raise ValueError(f"the pre-push hook refused {head[:12]}, exit {result}; see {log}")
                checked = head
            pushed = git(self.checkout, "push", "--no-verify", "origin", "HEAD:refs/heads/main")
            if pushed.returncode == 0:
                git(self.checkout, *FETCH)
                self.say(f"pushed {head[:12]}")
                for entry in batch: self.queue.settle(entry, LANDED, landedAs=head)
                return True
            said = pushed.stderr.strip()
            if "rejected" in said and ("fetch first" in said or "non-fast-forward" in said): continue
            if DROPPED.search(said) and not dropped:
                dropped = True
                self.say(f"the connection dropped during the push of {head[:12]}: pushing again")
                continue
            raise ValueError(f"the push of {head[:12]} was refused: {said or 'no output'}")
        return False

    def pre_push(self, head):
        """(status, log) of the project's pre-push hook for pushing head to main, called as Git would
        call it, before any connection to the remote. Status 0 when the project has none."""
        log = self.queue.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}-pre-push.log"
        told = self.queue.directory / "pre-push.stdin"
        told.write_text(f"HEAD {head} refs/heads/main {text(self.checkout, 'rev-parse', MAIN)}\n")
        url = text(self.checkout, "remote", "get-url", "--push", "origin")
        self.say(f"pre-push hook on {head[:12]}")
        try:
            result = processes.run(["git", "hook", "run", "--ignore-missing", f"--to-stdin={told}", "pre-push", "--",
                                    "origin", url], cwd=self.checkout, log_path=log, timeout=LANE_LIMIT, env=self.environment)
        except processes.TimedOut as error:
            self.say(f"pre-push hook: unmeasured: {error}")
            return evidence.UNMEASURED, log
        self.say(f"pre-push hook exited {result}")
        return result, log

    def failed(self, applied, log):
        """Bisect: the oldest half next. A lone entry that fails is the one to send back."""
        if len(applied) == 1:
            why = "the queue's lanes fail on this commit on top of origin/main: fix it and add it again"
            self.say(f"{applied[0]['id']}: {why}")
            self.queue.settle(applied[0], FAILED, why=why, log=str(log))
            self.limit, self.suspects = None, 0
            return
        self.queue.mark(applied, QUEUED)
        self.suspects = len(applied)
        self.limit = max(1, len(applied) // 2)
        self.say(f"{len(applied)} commits fail together: next the oldest {self.limit}")

    def passed(self, count):
        self.suspects = max(0, self.suspects - count)
        self.limit = self.suspects or None

    def round(self):
        queue = self.queue
        batch = queue.waiting()
        if self.limit: batch = batch[:self.limit]
        if not batch: return False
        git(queue.main_checkout, *FETCH, check=True)
        self.prepare()
        self.toolkit()
        base = text(queue.main_checkout, "rev-parse", MAIN)
        applied = self.integrate(batch, base)
        if not applied:
            self.limit, self.suspects = None, 0
            return True
        # The project's lanes and settings as they stand on main, so that no branch chooses its own gate.
        self.reset(base)
        config = load(self.checkout)
        for entry in applied:
            if self.pick(entry): raise ValueError(f"{entry['id']} applied a moment ago and no longer does")
        # Until main names any, the batch's: the commit that first names a project's lanes has to land.
        try: lanes_of(config)
        except ValueError:
            config = load(self.checkout)
            # The batch may name its lanes, but only main may say the hook need not run.
            config.get("queue", {}).pop("hookCoveredBy", None)
        queue.mark(applied, TESTING)
        head = self.head()
        self.say(f"testing {len(applied)} entr{'y' if len(applied) == 1 else 'ies'} at {head[:12]} on {base[:12]}")
        round_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{head[:8]}"
        result, log = self.gate(config, round_name)
        self.prune()
        if result in NO_VERDICT:
            queue.mark(applied, QUEUED)
            self.retries += 1
            if self.retries > RETRIES:
                queue.hold(f"the lanes gave no verdict, exit {result}, {self.retries} times; see {log}, then run "
                           "`eng queue resume`")
            else: self.say(f"no verdict (exit {result}): trying again")
            return True
        self.retries = 0
        if result:
            self.failed(applied, log)
            return True
        # An entry removed during the gate was part of what passed, so what is left has not been tested.
        current = [queue.read(entry["id"]) for entry in applied]
        if any(entry is None or entry["state"] not in WAITING for entry in current):
            self.say("an entry was removed during the run: gating what is left")
            queue.mark(applied, QUEUED)
            return True
        if self.push(config, applied, base): self.passed(len(applied))
        else: queue.mark(applied, QUEUED)
        return True

    def prune(self):
        logs = sorted(self.queue.logs.iterdir(), key=lambda path: path.stat().st_mtime)
        for path in logs[:-KEPT_LOGS]:
            with contextlib.suppress(OSError): path.unlink()

    def drain(self):
        while not self.queue.held() and self.round(): pass


def run(root):
    """Be the repository's worker until nothing waits. A second worker exits at once. Anything
    unexpected holds the queue, with every entry still waiting, rather than guessing at a verdict."""
    queue = Queue(root)
    while True:
        with queue.worker() as free:
            if not free: return 0
            # Whatever a worker that died left testing was never pushed.
            queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
            worker = Worker(queue)
            try:
                with processes.forwarding_signals(): worker.drain()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                queue.mark([entry for entry in queue.all() if entry["state"] == TESTING], QUEUED)
                queue.hold(f"{error}; then run `eng queue resume`")
                return 1
        # An entry added after the last look, while its adder found this worker running, is this
        # worker's to take: its adder's own worker has already exited.
        if queue.held() or not queue.waiting(): return 0


def resume(root):
    queue = Queue(root)
    with contextlib.suppress(OSError): queue.hold_file.unlink()
    queue.start()
    print(f"{len(queue.waiting())} waiting; worker {'running' if queue.working() else 'not needed'}")
    return 0


def land_arguments(parser):
    parser.add_argument("reference", nargs="?", help="the commit or branch; HEAD by default, and required with --repo")
    parser.add_argument("--repo", help="another project to land in: its path, or its name beside this project")
    parser.add_argument("--wait", action="store_true", help="follow it to its result and print the landed commit: "
                        "0 landed, 1 came back, 75 held or still waiting at the limit")
    parser.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    parser.add_argument("--note", help="what the entry is, for `eng queue status`")


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    adding = operations.add_parser("add", help="queue the commits a ref has and origin/main lacks, and return at once")
    adding.add_argument("reference", nargs="?", default="HEAD")
    adding.add_argument("--note", help="what the entry is, for `eng queue status`")
    state = operations.add_parser("status", help="say what is waiting, what is running and what came back")
    state.add_argument("--json", action="store_true")
    following = operations.add_parser("wait", help="follow an entry to its result: 0 landed, 1 came back, 75 "
                                      "held or still waiting at the limit")
    following.add_argument("id")
    following.add_argument("--limit", type=float, default=WAIT_LIMIT, metavar="SECONDS")
    removing = operations.add_parser("remove", help="take an entry out; a run in progress finishes, and lands nothing")
    removing.add_argument("id")
    operations.add_parser("resume", help="clear a hold and start the worker")
    operations.add_parser("run", help="be the worker in the foreground; `add` starts one by itself")


def main(root, args):
    if args.operation == "add":
        add(root, args.reference, args.note)
        return 0
    if args.operation == "status": return status(root, args.json)
    if args.operation == "wait": return wait(root, args.id, args.limit)
    if args.operation == "remove": return remove(Queue(root), args.id)
    if args.operation == "resume": return resume(root)
    return run(root)
