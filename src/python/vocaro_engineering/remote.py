"""Run a lane or an admitted command on the build host: a second Mac reached over SSH.

Nothing here takes a slot on this host. The exact commit, and the toolkit commit that runs it,
go to bare repositories on the build host by git over SSH, never through GitHub. A working tree
with uncommitted changes goes as a snapshot commit on HEAD, which no branch holds. build_host.py,
sent along and run there by the host's own Python, checks them out in a persistent checkout,
so builds stay warm, and runs `eng run` or `eng admit` under the build host's own heavy-work
admission: that host's queue, not a private pool beside this one. The log and the result come
back to this checkout's .build/engineering/remote, and a lane group's lanes' logs and receipts
with them once the run there ends. A whole lane's result at a commit is also kept by commit in
the repository's Git directory, where every worktree of the repository can find it; a
snapshot's is not.

`eng run` sends a lane marked buildHost here first: when the build host declines it before
anything runs, because its queue or every checkout is busy, the lane runs on this Mac instead.
A configuration problem, such as a build host that cannot be reached or has another Xcode, stops
the lane for the owner's decision (agents/core.md, Shared resources); only `--here` then runs it
here. `eng doctor` compares the two Macs, and `eng remote setup --runtimes` gives both the same
simulator runtimes. Before it sends work, and in `eng doctor`, it lists the processes there that
earlier runs left running, which nothing stops.
"""
import base64
import binascii
import contextlib
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import build_host, disk, processes
from .config import TOOLKIT_ROOT
from .resources import BUSY

# The SSH destination when neither --host nor VOCARO_BUILD_HOST names one: an alias the owner
# defines in ~/.ssh/config, so no machine's name is written here.
DEFAULT_HOST = "build-host"
# The build host's directory for repositories and checkouts, relative to its home.
ROOT = "Development/build-host"
# Persistent checkouts per project on the build host; more wait for one to come free.
CHECKOUTS = 4
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
       "-o", "ServerAliveCountMax=4"]
REF = "refs/vocaro/build-host/"
# Seconds a stopped run has to end on the build host before the connection is closed.
STOP_WAIT = 120
# What a Git hook exports for its own repository must not reach another.
GIT_LOCATION = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX"}
# `eng run`'s status when the build host's configuration stopped a lane: sysexits' EX_CONFIG.
MISCONFIGURED = 78
# Seconds `eng doctor` waits for the build host to report its Xcodes and runtimes.
SURVEY_TIMEOUT = 180
# Seconds `eng doctor` waits for the census of the build host's leftover processes.
CENSUS_TIMEOUT = 60
# Where a leftover process is explained.
LEFTOVER_GUIDE = "guides/shared-host.md, Processes left over from runs"
# The author and committer of a snapshot commit, which records a working tree rather than anyone's
# work, and needs no identity configured in the session's git.
SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "eng snapshot", "GIT_AUTHOR_EMAIL": "snapshot@vocaro-engineering.invalid",
                     "GIT_COMMITTER_NAME": "eng snapshot", "GIT_COMMITTER_EMAIL": "snapshot@vocaro-engineering.invalid"}


# What every project's gates use on the build host (guides/shared-host.md, Setting up a build host).
SETUP_FORMULAE = ["python", "uv", "shellcheck", "xcodegen", "zstd"]
# Where Homebrew's installer puts brew: Apple silicon, then Intel.
BREW = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
# The exit status of the setup script when the build host has no Homebrew.
NO_HOMEBREW = 4
FORMULA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+/-]*$")
MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# How messages name this Mac beside the build host.
HERE = "this Mac"
# Where a runtime image copied from the other Mac is staged until simctl adds it: on the build host
# under ~/Development/build-host, and here among the toolkit's durable state, never in /private/tmp,
# which a reboot empties. Both are relative to the Mac's home.
RUNTIME_STAGING = f"{ROOT}/runtimes"
LOCAL_RUNTIME_STAGING = "Library/Application Support/vocaro-engineering/runtimes"
# Free bytes a runtime's copy or download must leave on the volume that receives it: room for simctl
# to copy the image once more where it cannot clone it, as its help says it may, and for the 8-14 GB
# building worktrees of the sessions that share the volume (agents/core.md, Concurrent work).
RUNTIME_MARGIN = 40 << 30
# The bytes a download is taken to need when no image says: more than the largest runtime image on
# either Mac, iOS 26.5's 8.5 GB (October 2026).
RUNTIME_ESTIMATE = 10 << 30
# The platforms whose runtimes `xcodebuild -downloadPlatform` downloads (Xcode 27.2's help).
DOWNLOADABLE = {"iOS", "watchOS", "tvOS", "visionOS"}
# A runtime as build_host.toolchain names it: NAME (BUILD).
RUNTIME = re.compile(r"^(.*) \(([^()]+)\)$")


class Unavailable(RuntimeError):
    """The build host did not take the work, and nothing of it ran there."""


class Misconfigured(Unavailable):
    """The build host's configuration kept the work from running there: the owner's to fix, and
    no reason to run it on this Mac without the owner's decision (agents/core.md, Shared resources).
    problem says what is wrong, and fix what fixing it takes."""

    def __init__(self, problem, fix):
        super().__init__(f"{problem}; to fix it, {fix}")
        self.problem, self.fix = problem, fix


def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def unreachable(host, detail):
    return Misconfigured(f"{host} cannot be reached: {detail or 'ssh exited 255'}",
                         f"wake {host} and connect it to the network, or correct its entry in ~/.ssh/config or "
                         f"its authorized_keys, until `ssh {host} true` succeeds without a prompt "
                         "(guides/shared-host.md, Setting up a build host)")


def no_toolkit(host, status, detail):
    return Misconfigured(f"{host} could not run the toolkit (exit {status}): {detail or 'no output'}",
                         f"give {host}'s login shell a python3 that runs it: Homebrew's, which `eng remote setup` "
                         "installs, or the system's, which Xcode's command line tools provide "
                         "(guides/shared-host.md, Setting up a build host)")


def configured():
    """The build host to send buildHost lanes to first, or None for this Mac only.

    VOCARO_BUILD_HOST names it, and `none` turns it off. Otherwise it is the ssh alias, when
    ~/.ssh/config defines one: `ssh -G` resolves an undefined alias to its own name.
    """
    named = os.environ.get("VOCARO_BUILD_HOST")
    if named is not None:
        return None if named.strip().lower() in {"", "none", "off"} else named
    try:
        resolved = subprocess.run(["ssh", "-G", DEFAULT_HOST], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.split(None, 1)[1] for line in resolved.stdout.splitlines() if line.startswith("hostname ")]
    return DEFAULT_HOST if resolved.returncode == 0 and names and names[0] != DEFAULT_HOST else None


def environment():
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}


def git(root, *arguments, variables=None):
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                            env=variables or environment(), timeout=120)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} in {root} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def committed(root, untracked):
    """HEAD of a checkout with nothing uncommitted, as the toolkit's must be: only the project's
    working tree goes as a snapshot."""
    status = git(root, "status", "--porcelain", f"--untracked-files={'normal' if untracked else 'no'}")
    if status:
        raise Unavailable(f"{root} has changes the build host would not run; commit them first:\n{status}")
    return git(root, "rev-parse", "HEAD")


def snapshot(root):
    """The commit that holds the checkout's working tree, and HEAD: HEAD itself when the tree is
    HEAD's, otherwise a snapshot commit with HEAD as its parent, which no branch holds.

    `git add -A` takes the working tree into a temporary index, a copy of the checkout's own:
    tracked changes, deletions and nonignored untracked files, and no ignored file, as a commit
    would. The session's own index, HEAD, branch and working tree are untouched."""
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}")
    index = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="eng-snapshot-") as directory:
        variables = {**environment(), "GIT_INDEX_FILE": str(Path(directory) / "index")}
        # The copy keeps the index's file stats, so git add reads only the files that changed.
        if index.is_file(): shutil.copyfile(index, variables["GIT_INDEX_FILE"])
        else: git(root, "read-tree", head, variables=variables)
        git(root, "add", "-A", variables=variables)
        tree = git(root, "write-tree", variables=variables)
    if tree == git(root, "rev-parse", f"{head}^{{tree}}"): return head, head
    commit = git(root, "commit-tree", tree, "-p", head, "-m", f"Snapshot of the working tree on {head}",
                 variables={**environment(), **SNAPSHOT_IDENTITY})
    return commit, head


def common_directory(root):
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def checkout_name(root):
    """The main checkout's directory name, from a linked worktree too."""
    common = common_directory(root)
    return common.parent.name if common.name == ".git" else Path(root).resolve().name


def origin(root):
    """The toolkit's public origin for the build host's policy refresh, which needs no GitHub key."""
    try: url = git(root, "config", "--get", "remote.origin.url")
    except RuntimeError: return None
    return "https://github.com/" + url[len("git@github.com:"):] if url.startswith("git@github.com:") else url


def developer_directory():
    selected = os.environ.get("DEVELOPER_DIR") or build_host.output(["xcode-select", "-p"])
    return selected if selected and Path(selected).is_dir() else None


def login(script):
    """Have the build host's login shell run script, so its PATH is Terminal's, Homebrew's included."""
    return f'exec "$SHELL" -l -c {shlex.quote(script)}'


def ssh(host, script, **options):
    return subprocess.run([*SSH, host, login(script)], env=environment(), **options)


def send(root, host, repository, commit):
    """Push one commit by name to a bare repository on the build host, without the push hooks:
    this is no publication, and a pre-push hook would run the gates this offloads.

    Sessions that send one commit at once race for its ref, and the push that loses fails with
    "reference already exists". The commit is then there all the same, so that push has sent it."""
    variables = {**environment(), "GIT_SSH_COMMAND": shlex.join(SSH)}
    result = subprocess.run(["git", "-C", str(root), "push", "--no-verify", "--quiet", "--receive-pack=git receive-pack",
                             f"{host}:{repository}", f"{commit}:{REF}{commit}"], env=variables,
                            capture_output=True, text=True)
    if result.returncode:
        held = subprocess.run(["git", "-C", str(root), "ls-remote", "--upload-pack=git upload-pack",
                               f"{host}:{repository}", f"{REF}{commit}"], env=variables, capture_output=True, text=True)
        if held.returncode == 0 and held.stdout.split()[:1] == [commit]: return
        raise Misconfigured(f"{host} did not receive {commit[:12]} into ~/{repository} (git push exited "
                            f"{result.returncode}): {last_line(result.stderr) or 'no output'}",
                            f"repair ~/{repository} on {host}, or free space there, until the push succeeds")


def store(root):
    return common_directory(root) / "vocaro-engineering/remote"


def passed(root, lane, revision="HEAD"):
    """The receipt of a passing build-host run of the whole lane at exactly revision's commit, or None.

    A snapshot's run never counts, for HEAD or for the snapshot itself: no branch holds it."""
    commit = git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    try:
        receipt = json.loads((store(root) / commit / f"{lane}.json").read_text())
    except (OSError, ValueError):
        return None
    result = receipt.get("result", {})
    whole = receipt.get("commit") == commit and receipt.get("lane") == lane and not receipt.get("arguments") \
        and not receipt.get("snapshot")
    return receipt if whole and result.get("exitStatus") == 0 and not result.get("stopped") else None


def write(stream, data):
    try: stream.buffer.write(data)
    except AttributeError: stream.write(data.decode(errors="replace"))
    stream.flush()


def describe(identity):
    xcode = (identity.get("xcode") or "no Xcode").splitlines()
    return (f"{identity.get('name')} ({identity.get('model')}, macOS {identity.get('macOS')} "
            f"({identity.get('build')}), {', '.join(xcode)})")


def group_files(config, lane):
    """What comes back from a lane group's run beside its own output and receipt: each of its lanes'
    log and receipt, as paths relative to the group's results, lanes/LANE.log and lanes/LANE.json.
    None for a lane that is no group."""
    members = config.get("lanes", {}).get(lane, {}).get("parallel") if lane is not None else None
    return [f"lanes/{member}{suffix}" for member in members for suffix in (".log", ".json")] if members else None


def execute(root, config, host, work, *, lane=None, arguments=(), expect=None, decline=False):
    """Run eng's work on the build host and return its exit status, with the log and receipt kept here.

    work is the build host's eng arguments: run [--expect N] LANE [-- ARGUMENTS] or
    admit [--exclusive | --model] -- COMMAND. It runs at HEAD, or at a snapshot commit of the
    working tree when that has uncommitted changes. A lane group's lanes' logs and receipts come
    back under the results' lanes/ once the run there ends, however it ended (group_files). With
    decline, the build host refuses work it cannot start now rather than waiting for it, and
    Unavailable says why. Misconfigured says what of the build host's configuration kept the work
    from running there, and what fixing it takes.
    """
    toolkit = Path(TOOLKIT_ROOT).resolve()
    try:
        commit, head = snapshot(root)
        directory = checkout_name(root)
        kept = store(root) / commit
        kit = None
        project = {"directory": directory, "repository": f"repositories/{directory}.git", "commit": commit,
                   "ref": f"{REF}{commit}"}
        if toolkit == Path(root).resolve():
            project["origin"] = origin(root)
        else:
            kit_commit = committed(toolkit, untracked=False)
            kit_directory = checkout_name(toolkit)
            kit = {"directory": kit_directory, "repository": f"repositories/{kit_directory}.git",
                   "commit": kit_commit, "ref": f"{REF}{kit_commit}", "origin": origin(toolkit)}
    except RuntimeError as error:  # Git cannot name a commit to send, so nothing is sent.
        raise Unavailable(str(error)) from error
    developer = developer_directory()
    local = build_host.identity(developer)
    name = lane if lane is not None else "admit"
    returns = group_files(config, lane)
    plan = {"root": ROOT, "checkouts": CHECKOUTS, "project": project, "toolkit": kit, "work": work,
            "decline": decline, "busy": BUSY,
            "provision": "provision" in config.get("lanes", {}) and lane != "provision",
            "developerDir": developer, "xcode": local["xcode"],
            "log": f".build/engineering/{lane}/run.log" if lane is not None else None,
            "receipt": f".build/engineering/{name}/source.json",
            "returns": {"directory": f".build/engineering/{lane}", "paths": returns} if returns else None}

    repositories = [f"{ROOT}/{part['repository']}" for part in (project, kit) if part is not None]
    prepare = " && ".join([f"mkdir -p {shlex.quote(ROOT + '/repositories')}", *(
        f"{{ [ -d {shlex.quote(path)} ] || git init --bare --quiet {shlex.quote(path)}; }}" for path in repositories)])
    at = commit[:12] if commit == head else f"{commit[:12]}, a snapshot of the working tree on {head[:12]},"
    print(f"remote: sending {at} to {host}", file=sys.stderr, flush=True)
    # The connection that prepares the repositories also lists what earlier runs left running there,
    # before any work is sent: a connection to the build host can take seconds of its own.
    prepared = ssh(host, f"{prepare} || exit $?\n{census_command()} || true", stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
    # ssh's own failures, a name that does not resolve among them, exit 255.
    if prepared.returncode == 255: raise unreachable(host, last_line(prepared.stderr))
    if prepared.returncode:
        raise Misconfigured(f"{host} could not prepare ~/{ROOT} (exit {prepared.returncode}): "
                            f"{last_line(prepared.stderr) or 'no output'}",
                            f"make ~/{ROOT} writable there and install the Xcode this Mac uses, with its license "
                            "accepted and its first launch run (guides/shared-host.md, Setting up a build host)")
    for line in leftover_lines(host, *read_census(prepared)):
        print(f"remote: {line}", file=sys.stderr, flush=True)
    send(root, host, repositories[0], commit)
    if kit is not None: send(toolkit, host, repositories[1], kit["commit"])
    if processes.requested() is not None:
        raise processes.Interrupted(processes.requested(), "before the build host started the run")

    results = Path(root) / ".build/engineering/remote" / name
    # Results are staged, so a refusal leaves the previous run's whole.
    staging = results.with_name(f".{name}.{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    started = time.time()
    source = Path(build_host.__file__).read_text()
    command = [*SSH, host, login(shlex.join(["python3", "-c", source, json.dumps(plan)]))]
    lines, result, stopping = queue.Queue(), None, None
    # Whether build_host.py started there, and the last line of anything else, such as the shell's.
    framed, stray = False, None
    # The files asked back that have arrived, in order, and the one arriving now.
    returned, receiving = [], None
    with (staging / "run.log").open("wb") as log, (staging / "output.log").open("wb") as output:
        channel = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env=environment())
        def relay():
            for line in iter(channel.stdout.readline, b""): lines.put(line)
            lines.put(None)
        threading.Thread(target=relay, daemon=True).start()
        while True:
            if processes.requested() is not None and stopping is None:
                stopping = time.monotonic()
                print(f"remote: stop requested; asking {host} to stop the run", file=sys.stderr, flush=True)
                # The end of the build host's standard input is its stop.
                try: channel.stdin.close()
                except OSError: pass
            if stopping is not None and time.monotonic() - stopping > STOP_WAIT and channel.poll() is None:
                print(f"remote: {host} did not finish stopping in {STOP_WAIT}s; closing the connection",
                      file=sys.stderr, flush=True)
                channel.terminate()
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None: break
            kind, data = line[:2], line[2:]
            framed = framed or kind in (b"L ", b"R ", b"O ", b"F ", b"D ")
            if kind == b"L ":
                log.write(data)
            elif kind == b"R ":
                result = json.loads(data)
            elif kind == b"F ":
                if receiving is not None: receiving.close()
                receiving = None
                try: path = json.loads(data)["path"]
                except (ValueError, TypeError, KeyError): path = None
                # Only a file the plan asked back, by its exact path, and each once.
                if isinstance(path, str) and path in (returns or []) and path not in returned:
                    (staging / path).parent.mkdir(parents=True, exist_ok=True)
                    receiving = (staging / path).open("wb")
                    returned.append(path)
            elif kind == b"D ":
                if receiving is not None:
                    try:
                        receiving.write(base64.b64decode(data))
                    except binascii.Error:
                        receiving.close()
                        receiving = None
                        damaged = returned.pop()
                        (staging / damaged).unlink()
                        print(f"remote: {damaged} came back damaged and was not kept", file=sys.stderr, flush=True)
            else:
                data = data if kind == b"O " else line
                if kind != b"O ": stray = last_line(line.decode(errors="replace")) or stray
                output.write(data)
                write(sys.stdout, data)
        channel.wait()
        if receiving is not None: receiving.close()
    if result is None and not framed and stopping is None:
        # build_host.py never started, so nothing ran there.
        shutil.rmtree(staging, ignore_errors=True)
        if channel.returncode == 255: raise unreachable(host, stray)
        raise no_toolkit(host, channel.returncode, stray)
    if result is not None and result.get("refused"):
        shutil.rmtree(staging, ignore_errors=True)
        if result.get("busy"): raise Unavailable(f"{host} did not take it: {result['refused']}")
        raise Misconfigured(f"{host} refused it: {result['refused']}",
                            result.get("fix") or f"repair on {host} what that names")
    shutil.rmtree(results, ignore_errors=True)
    staging.rename(results)
    if result is None:
        raise RuntimeError(f"No result from {host} (ssh exited {channel.returncode}); the run is unmeasured. "
                           f"Its output is in {results / 'output.log'}")
    # commit is what ran there: HEAD, or a snapshot of the working tree on it.
    receipt = {"host": host, "commit": commit, "head": head, "snapshot": commit != head,
               "toolkit": kit["commit"] if kit else commit, "lane": lane, "arguments": list(arguments),
               "expect": expect, "work": work, "local": local, "result": result, "dispatched": started,
               **({"returned": returned} if returns else {})}
    (results / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")
    # A snapshot's result stays here: no branch holds it, so it is no commit's pass.
    if lane is not None and not arguments and expect is None and commit == head:
        kept.mkdir(parents=True, exist_ok=True)
        (kept / f"{lane}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if result.get("error"):
        raise RuntimeError(f"{host}: {result['error']}")
    status = result["exitStatus"]
    remote = result["host"]
    print(f"remote: {name} at {at} on {describe(remote)} in {result.get('checkout')}: "
          f"{'stopped' if result['stopped'] else f'exit {status}'}; results in {results}", file=sys.stderr, flush=True)
    if returns and result.get("stage") == "work":
        members = config["lanes"][lane]["parallel"]
        came = [member for member in members if f"lanes/{member}.log" in returned]
        if came:
            print(f"remote: the logs and receipts of {name}'s lanes are in {results / 'lanes'}: {', '.join(came)}",
                  file=sys.stderr, flush=True)
        if len(came) < len(members):
            print(f"remote: no log came back for {', '.join(member for member in members if member not in came)}",
                  file=sys.stderr, flush=True)
    if (remote.get("macOS"), remote.get("build")) != (local.get("macOS"), local.get("build")):
        print(f"remote: the build host runs macOS {remote.get('macOS')} ({remote.get('build')}) and this Mac "
              f"{local.get('macOS')} ({local.get('build')}): the result qualifies the build host's platform",
              file=sys.stderr, flush=True)
    if result.get("stage") == "provision" and status and not result["stopped"]:
        raise Misconfigured(f"{host} could not provision its checkout: the provision lane exited {status} in "
                            f"{result.get('checkout')}, so {name} did not run there; its log is {results / 'run.log'}",
                            f"give {host} what provisioning needs, which that log names, such as a tool "
                            "`eng remote setup` installs or the git-credentials token for private dependencies "
                            "(guides/shared-host.md, Setting up a build host)")
    if status is None: return 1
    # A command a signal ended has a negative status; a shell reports it as 128 plus the signal.
    return 128 - status if status < 0 else status


def main(root, config, args):
    if args.operation == "passed":
        receipt = passed(root, args.lane, args.commit)
        if receipt is None:
            print(f"No passing build-host run of the whole {args.lane} lane at {args.commit}")
            return 1
        print(f"{args.lane} passed at {receipt['commit']} on {describe(receipt['result']['host'])}")
        return 0
    host = args.host or os.environ.get("VOCARO_BUILD_HOST") or DEFAULT_HOST
    if args.operation == "setup": return setup(host, args.formulae, args.imports or [], args.check, args.runtimes)
    if args.operation == "run":
        lane = config.get("lanes", {}).get(args.lane)
        if lane is None: raise ValueError(f"No configured lane: {args.lane}")
        if lane.get("foreground"): raise ValueError(f"{args.lane} takes the screen; foreground lanes run only here")
        if args.expect is not None and args.expect < 1: raise ValueError("--expect needs at least 1 test")
        extra = args.passed or []
        work = ["run", "--here", *(["--expect", str(args.expect)] if args.expect else []), args.lane,
                *(["--", *extra] if extra else [])]
        with processes.forwarding_signals():
            return execute(root, config, host, work, lane=args.lane, arguments=extra, expect=args.expect)
    # Without a leading --, argparse collected the command, and a later -- belongs to it.
    command = args.command + ([] if args.passed is None else ["--", *args.passed]) if args.command else args.passed
    if not command: raise ValueError("Name the command: eng remote admit [--exclusive | --model] -- COMMAND")
    kind = ["--exclusive"] if args.exclusive else ["--model"] if args.model else []
    with processes.forwarding_signals():
        return execute(root, config, host, ["admit", *kind, "--", *command])


def install_script(formulae):
    """Find Homebrew, put it first on the login shell's PATH in ~/.zprofile, and install formulae.

    Installing Homebrew itself needs the build host's password, which only the owner types, so a
    build host without it ends the script with NO_HOMEBREW and nothing changed."""
    candidates = " ".join(shlex.quote(candidate) for candidate in BREW)
    return f"""set -u
brew=$(command -v brew || true)
for candidate in {candidates}; do
    if [ -z "$brew" ] && [ -x "$candidate" ]; then brew=$candidate; fi
done
if [ -z "$brew" ]; then
    echo "setup: Homebrew is not installed on this build host. Its installer asks for the build host's password, so install it there yourself (https://brew.sh), then run eng remote setup again." >&2
    exit {NO_HOMEBREW}
fi
# Homebrew's bin must come before /usr/bin, or the system's python3 3.9 shadows Homebrew's, as it
# did in the build host's login shell, which found brew after /usr/bin (2026-10-26).
prefix=$("$brew" --prefix)
first=no
for entry in $(printf '%s' "$PATH" | tr ':' ' '); do
    if [ "$entry" = "$prefix/bin" ]; then first=yes; break; fi
    if [ "$entry" = /usr/bin ]; then break; fi
done
if [ "$first" = no ]; then
    line='eval "$('"$brew"' shellenv)"'
    grep -qxF "$line" "$HOME/.zprofile" 2>/dev/null || printf '\\n%s\\n' "$line" >> "$HOME/.zprofile"
    eval "$("$brew" shellenv)"
    echo "setup: put Homebrew first on the login shell's PATH in ~/.zprofile"
fi
echo "setup: brew install {' '.join(formulae)}"
"$brew" install {' '.join(shlex.quote(formula) for formula in formulae)}
"""


def verify_script(formulae, modules, developer=None):
    """One `ok` or `MISSING` line for each thing the gates need, read by a fresh login shell, as
    `eng remote` reads the build host; exit 1 when anything is missing. The last line is the
    version of the Xcode at developer, the path a run there uses, which the caller compares."""
    lines = ["fail=0",
             "prefix=$(brew --prefix 2>/dev/null) || { echo \"MISSING Homebrew on the login shell's PATH\"; exit 1; }",
             'echo "ok      Homebrew at $prefix"',
             "python=$(command -v python3 || true)",
             'case "$python" in "$prefix"/*) echo "ok      python3 is $python, $(python3 --version 2>&1)" ;;',
             "    *) echo \"MISSING Homebrew's python3 first on the login shell's PATH (found ${python:-none})\"; fail=1 ;;",
             "esac"]
    # `brew info` follows an alias such as python to its formula, where `brew list` refuses it. The
    # system's Python reads its JSON, which Homebrew's may not yet be there to do.
    installed = "/usr/bin/python3 -c 'import json, sys; sys.exit(not json.load(sys.stdin)[\"formulae\"][0][\"installed\"])'"
    for formula in formulae:
        lines.append(f'if brew info --json=v2 {formula} 2>/dev/null | {installed}; then echo "ok      {formula}"; '
                     f'else echo "MISSING {formula}"; fail=1; fi')
    for module in modules:
        lines.append(f'if python3 -c "import {module}" >/dev/null 2>&1; then echo "ok      import {module}"; '
                     f'else echo "MISSING import {module} in python3"; fail=1; fi')
    selected = f"DEVELOPER_DIR={shlex.quote(developer)} " if developer else ""
    lines += [f'echo "xcode   $({selected}xcrun xcodebuild -version 2>/dev/null | tr \'\\n\' \' \')"', 'exit $fail']
    return "\n".join(lines) + "\n"


def setup(host, formulae, modules, check, runtimes=False):
    """Install what the gates use on the build host, then check it as `eng remote` will see it.
    With check, only check. With runtimes, give both Macs the same simulator runtimes instead
    (match_runtimes)."""
    if runtimes:
        if formulae or modules:
            raise ValueError("--runtimes brings only the simulator runtimes to their union; name formulae and "
                             "--import modules in a setup of their own")
        return match_runtimes(host, check)
    for name in formulae:
        if not FORMULA.match(name): raise ValueError(f"Not a Homebrew formula name: {name}")
    for name in modules:
        if not MODULE.match(name): raise ValueError(f"Not a Python module name: {name}")
    wanted = list(dict.fromkeys([*SETUP_FORMULAE, *formulae]))
    if not check:
        # Line by line through this process, so a long brew install shows as it goes.
        with subprocess.Popen([*SSH, host, login(install_script(wanted))], env=environment(), text=True,
                              errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as running:
            for line in running.stdout: print(line, end="", flush=True)
        installed = running.returncode
        if installed == NO_HOMEBREW: return 1
        if installed:
            print(f"eng: brew install on {host} exited {installed}", file=sys.stderr)
            return 1
    # A new login shell, so a PATH line just written to ~/.zprofile is read as eng remote reads it.
    # A run there uses the Xcode at this Mac's developer directory, and must find the same version.
    developer = developer_directory()
    here = " ".join((build_host.output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer})
                     or "").split()) if developer else None
    result = ssh(host, verify_script(wanted, modules, developer), capture_output=True, text=True)
    status = 0 if result.returncode == 0 else 1
    for line in result.stdout.splitlines():
        if line.startswith("xcode   "):
            there = " ".join(line[8:].split())
            if here and there != here:
                line, status = f"MISSING {here} at {developer}, as on this Mac (found {there or 'none'})", 1
            else:
                line = f"ok      {there or 'no Xcode'}" + (f" at {developer}" if developer else "")
        print(line)
    if result.returncode not in (0, 1):
        print(f"eng: could not check {host} (ssh exited {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
    print(f"{host} is ready for eng remote" if status == 0 else f"{host} is not ready: install what is MISSING")
    return status


def gib(size):
    return f"{size / (1 << 30):.1f} GiB"


def runtime_differences(here, there, host):
    """What only this Mac's and only host's list of runtimes names, as `eng doctor` says it."""
    only = lambda side, other: [runtime for runtime in side if runtime not in other]
    return [f"only {name} has {', '.join(runtimes)}" for name, runtimes in
            [(HERE, only(here, there)), (host, only(there, here))] if runtimes]


def toolchains(host, developer):
    """Each Mac's build_host.toolchain with the Xcode at this Mac's developer directory."""
    return {HERE: build_host.toolchain(developer), host: survey(host, developer)}


def runtime_images(host, side, developer, clear=False):
    """A Mac's runtime images, the free space where a copy to it is staged, and what an earlier copy
    left staged there, which clear removes: build_host.images and build_host.stage on side."""
    if side == HERE:
        return {"images": build_host.images(developer), **build_host.stage(Path.home() / LOCAL_RUNTIME_STAGING, clear)}
    return ask(host, {"images": developer, "staging": RUNTIME_STAGING, "clear": clear})


def runtime_lock():
    """An flock that keeps a second runtime copy from starting on this Mac while one runs, or None."""
    directory = Path.home() / LOCAL_RUNTIME_STAGING
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def runtime_plan(found, stores, host):
    """A step for each runtime that only one Mac lists: the runtime, its build, the Mac that has it
    (source), the one that lacks it (target), and how it comes there: a copy of the source's image
    when that can be read, else a download by its build, else None, with why."""
    plan = []
    for source, target in ((HERE, host), (host, HERE)):
        for runtime in sorted(set(found[source]["runtimes"]) - set(found[target]["runtimes"])):
            matched = RUNTIME.match(runtime)
            name, build = matched.groups() if matched else (runtime, None)
            step = {"runtime": runtime, "build": build, "source": source, "target": target}
            held = [image for image in stores[target]["images"] if image["build"] == build]
            images = [image for image in stores[source]["images"] if image["build"] == build]
            readable = [image for image in images if image["file"]]
            platform = (name.split() or [""])[0]
            why = images[0]["unreadable"] if images else f"{source} lists no disk image of it"
            if build is None:
                step.update(how=None, why="its name gives no build")
            elif held:
                step.update(how=None, why=f"{target} holds its image ({held[0]['state']}), which the Xcode there does "
                            "not list as available; see `xcrun simctl runtime list` there")
            elif readable:
                step.update(how="copy", image=readable[0], size=readable[0]["sizeBytes"])
            elif platform in DOWNLOADABLE:
                known = images and isinstance(images[0]["sizeBytes"], int)
                step.update(how="download", why=why, platform=platform,
                            size=images[0]["sizeBytes"] if known else RUNTIME_ESTIMATE,
                            architectures=images[0]["architectures"] if images else [])
            else:
                step.update(how=None, why=f"{why}, and xcodebuild downloads no {platform} runtime")
            plan.append(step)
    return plan


def reporter(runtime, size):
    """A copy's progress: a line at each tenth of the image. A requested stop ends the copy."""
    shown = [0]
    def report(done):
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"while copying {runtime}")
        if done - shown[0] >= size / 10 or done == size:
            shown[0] = done
            print(f"runtimes: {runtime}: {gib(done)} of {gib(size)} copied", flush=True)
    return report


def send_runtime(host, image, developer, report):
    """Send this Mac's image of a runtime to host, where build_host.receive stages it and simctl adds
    it. Return None once it is added there, else why not."""
    request = {"receive": {"staging": RUNTIME_STAGING, "build": image["build"], "size": image["sizeBytes"],
                           "developerDir": developer}}
    command = [*SSH, host, login(shlex.join(["python3", "-c", Path(build_host.__file__).read_text(),
                                             json.dumps(request)]))]
    failure = None
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=environment()) as channel:
        try:
            with open(image["file"], "rb") as source:
                sent = 0
                for chunk in iter(lambda: source.read(build_host.CHUNK), b""):
                    channel.stdin.write(chunk)
                    sent += len(chunk)
                    report(sent)
        except BrokenPipeError:
            pass  # The build host stopped reading, and its answer says why.
        except OSError as error:
            failure = f"reading {image['file']} failed: {error}"
        finally:
            # The end of its standard input ends the image there, and a short image is never added.
            with contextlib.suppress(OSError): channel.stdin.close()
        reply = channel.stdout.read().decode(errors="replace")
    if channel.returncode == 255: return f"{host} cannot be reached: {last_line(reply) or 'ssh exited 255'}"
    try:
        result = json.loads(last_line(reply))
    except (TypeError, ValueError):
        result = None
    if not isinstance(result, dict):
        return failure or f"{host} gave no answer (exit {channel.returncode}): {last_line(reply) or 'no output'}"
    return failure or result.get("failed")


def fetch_runtime(host, image, developer, report):
    """Fetch host's image of a runtime over SSH, then stage and add it here as build_host.receive does
    there. Return None once it is added here, else why not."""
    # cat needs no login shell, whose profile could write into the image.
    command = [*SSH, host, shlex.join(["cat", image["file"]])]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=environment()) as channel:
        try:
            failure = build_host.receive(channel.stdout, Path.home() / LOCAL_RUNTIME_STAGING, image["build"],
                                         image["sizeBytes"], developer, report)
        finally:
            channel.stdout.close()  # A stop ends cat too.
        error = channel.stderr.read().decode(errors="replace")
    if failure and channel.returncode:
        failure += (f"; {'ssh' if channel.returncode == 255 else 'cat'} exited {channel.returncode}: "
                    f"{last_line(error) or 'no output'}")
    return failure


def download_command(step):
    """xcodebuild's download of step's runtime by its build, for the architectures of its image."""
    architectures = set(step["architectures"])
    variant = ["-architectureVariant", "arm64"] if architectures == {"arm64"} else \
        ["-architectureVariant", "universal"] if {"arm64", "x86_64"} <= architectures else []
    return ["xcodebuild", "-downloadPlatform", step["platform"], "-buildVersion", step["build"], *variant]


def download_runtime(host, step, developer):
    """Download step's runtime with xcodebuild on the Mac that lacks it; return xcodebuild's status."""
    arguments = ["xcrun", *download_command(step)]
    if step["target"] == HERE:
        command, variables = arguments, {**os.environ, "DEVELOPER_DIR": developer}
    else:
        command = [*SSH, host, login(f"DEVELOPER_DIR={shlex.quote(developer)} {shlex.join(arguments)}")]
        variables = environment()
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", env=variables) as running:
        for line in running.stdout:
            print(f"runtimes: {step['target']}: {line.rstrip()}", flush=True)
            if processes.requested() is not None: running.terminate()
    if processes.requested() is not None:
        raise processes.Interrupted(processes.requested(), f"while downloading {step['runtime']}")
    return running.returncode


def match_runtimes(host, check):
    """Bring this Mac and host to the union of the simulator runtimes that the Xcode at this Mac's
    developer directory lists, as `eng doctor` compares them, and return 0 once both list the same.

    A runtime only one Mac lists goes to the other as its own disk image, over SSH: staged there,
    and added with `simctl runtime add --move`, so no second copy stays. One whose image cannot be
    read is downloaded there with xcodebuild, by its build. Nothing is ever deleted. A copy or
    download that would leave less than RUNTIME_MARGIN free where it lands is refused before it
    starts. Then both Macs' runtimes are listed again, and what still differs is named. With
    check, say what it would do and change nothing."""
    developer = developer_directory()
    if developer is None:
        print("runtimes: this Mac has no Xcode selected, so there are no runtimes to compare", flush=True)
        return 1
    lock = None
    if not check:
        lock = runtime_lock()
        if lock is None:
            print("runtimes: another `eng remote setup --runtimes` is running on this Mac; run it again once that "
                  "one ends", flush=True)
            return 1
    try:
        with processes.forwarding_signals():
            return bring_runtimes(host, developer, check)
    except Misconfigured as problem:
        print(f"runtimes: {problem}", flush=True)
        return 1
    finally:
        if lock is not None: os.close(lock)


def bring_runtimes(host, developer, check):
    """match_runtimes' work, once this Mac holds the lock that a change takes."""
    found = toolchains(host, developer)
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    if not found[HERE]["xcode"]:
        print(f"runtimes: the Xcode at {developer} does not run on this Mac", flush=True)
        return 1
    if found[HERE]["xcode"] != found[host].get("xcode"):
        there = version(found[host]["xcode"]) if found[host].get("xcode") else "missing"
        print(f"runtimes: the Xcode at {developer} is {version(found[HERE]['xcode'])} on this Mac and {there} on "
              f"{host}; install this Mac's Xcode at that path there first (guides/shared-host.md, Setting up a build "
              "host)", flush=True)
        return 1
    xcode = f"{version(found[HERE]['xcode'])} at {developer}"
    stores = {side: runtime_images(host, side, developer, clear=not check) for side in (HERE, host)}
    for side in (HERE, host):
        if found[side].get("runtimes") is None or stores[side]["images"] is None:
            print(f"runtimes: simctl did not list the runtimes on {side}", flush=True)
            return 1
        if stores[side]["left"] and not check:
            print(f"runtimes: removed {', '.join(stores[side]['left'])}, which an earlier copy left staged on {side}",
                  flush=True)
    plan = runtime_plan(found, stores, host)
    if not plan:
        runtimes = found[HERE]["runtimes"]
        print(f"runtimes: this Mac and {host} list the same {len(runtimes)} runtimes of {xcode}: "
              f"{', '.join(runtimes) or 'none'}", flush=True)
        return 0
    planned = {HERE: 0, host: 0}
    for step in plan:
        runtime, source, target = step["runtime"], step["source"], step["target"]
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"before {runtime} came to {target}")
        if step["how"] is None:
            print(f"runtimes: {runtime}: cannot come to {target}: {step['why']}", flush=True)
            continue
        size = step["size"]
        if check:
            free = stores[target]["free"] - planned[target]
            planned[target] += size
        else:
            # Measured again before each copy, which those before it have used space for.
            fresh = runtime_images(host, target, developer)
            if any(image["build"] == step["build"] for image in fresh["images"] or []):
                print(f"runtimes: {runtime}: {target} holds it now", flush=True)
                continue
            free = fresh["free"]
        heading = f"runtimes: {runtime}, {gib(size)}:"
        if free - size < RUNTIME_MARGIN:
            print(f"{heading} {'would be refused' if check else 'refused'} on {target}: {gib(free)} is free there, "
                  f"and {gib(size)} more would leave less than the {gib(RUNTIME_MARGIN)} it must keep free; free space "
                  "there, then run it again", flush=True)
            continue
        download = (f"with `{shlex.join(download_command(step))}`, since its image on {source} cannot be copied: "
                    f"{step['why']}") if step["how"] == "download" else None
        if check and download is None:
            print(f"{heading} would be copied from {source} to {target}, which has {gib(free)} free", flush=True)
        elif check:
            print(f"{heading} would be downloaded on {target}, which has {gib(free)} free, {download}", flush=True)
        elif download is None:
            print(f"{heading} copying from {source} to {target}", flush=True)
            failure = (send_runtime if target == host else fetch_runtime)(host, step["image"], developer,
                                                                            reporter(runtime, size))
            print(f"runtimes: {runtime}: added on {target}" if failure is None else
                  f"runtimes: {runtime}: not added on {target}: {failure}; nothing of it stays staged there", flush=True)
        else:
            print(f"{heading} downloading on {target} {download}", flush=True)
            status = download_runtime(host, step, developer)
            print(f"runtimes: {runtime}: {'downloaded' if status == 0 else f'xcodebuild exited {status}'} on {target}",
                  flush=True)
    if check:
        print(f"runtimes: {len(plan)} runtime(s) differ; `eng remote setup --runtimes` brings both Macs to their union",
              flush=True)
        return 1
    # What both Macs list now decides the outcome, not what each step reported.
    found = toolchains(host, developer)
    for side in (HERE, host):
        if found[side].get("runtimes") is None:
            print(f"runtimes: simctl did not list the runtimes again on {side}", flush=True)
            return 1
    here, there = found[HERE]["runtimes"], found[host]["runtimes"]
    if here == there:
        print(f"runtimes: this Mac and {host} list the same {len(here)} runtimes of {xcode}: {', '.join(here)}",
              flush=True)
        return 0
    print(f"runtimes: the runtimes of {xcode} still differ: {'; '.join(runtime_differences(here, there, host))}",
          flush=True)
    return 1


def first(root, config, lane, arguments, expect, fallback=True):
    """Run a buildHost lane on the build host if it takes it now and return its status.

    A tree with uncommitted changes goes as a snapshot commit (execute). The build host declines a
    lane when none is configured or its queue or every checkout is busy, and nothing is sent when
    Git cannot name the commits to send, as when the toolkit's checkout has changes to tracked files.
    After saying why, return None, for the caller to run the lane on this Mac, or without fallback
    BUSY. A configuration problem of the build host's stops the lane instead: return MISCONFIGURED
    after saying what is wrong and what fixing it takes.
    """
    host = configured()
    if host is None:
        if fallback: return None
        print(f"eng: {lane} not run: no build host is configured (VOCARO_BUILD_HOST, or the ssh alias "
              f"{DEFAULT_HOST} in ~/.ssh/config)", file=sys.stderr, flush=True)
        return BUSY
    work = ["run", "--here", "--if-free", *(["--expect", str(expect)] if expect else []), lane,
            *(["--", *arguments] if arguments else [])]
    try:
        with processes.forwarding_signals():
            return execute(root, config, host, work, lane=lane, arguments=arguments, expect=expect, decline=True)
    except Misconfigured as stop:
        print(f"eng: {lane} not run: {stop.problem}\neng: to fix it, {stop.fix}\neng: a misconfigured shared host "
              f"stops the work that depends on it (agents/core.md, Shared resources). Running {lane} on this Mac "
              f"instead takes the owner's decision, then `eng run --here {lane}`.", file=sys.stderr, flush=True)
        return MISCONFIGURED
    except Unavailable as reason:
        print(f"eng: {f'running {lane} on this Mac' if fallback else f'{lane} not run'}: {reason}",
              file=sys.stderr, flush=True)
        return None if fallback else BUSY


def ask(host, request):
    """build_host.py's answer to request on the build host, run by its login shell as a run there is."""
    request = shlex.join(["python3", "-c", Path(build_host.__file__).read_text(), json.dumps(request)])
    try:
        found = ssh(host, request, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SURVEY_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise unreachable(host, f"it did not answer in {SURVEY_TIMEOUT}s") from None
    except OSError as error:
        raise unreachable(host, str(error)) from None
    if found.returncode == 255: raise unreachable(host, last_line(found.stderr))
    try:
        return json.loads(last_line(found.stdout))
    except (TypeError, ValueError):
        raise no_toolkit(host, found.returncode, last_line(found.stderr)) from None


def survey(host, developer):
    """The build host's build_host.toolchain, read in its login shell as a run there reads it."""
    return ask(host, {"toolchain": developer})


def census_command():
    """The build host's command that prints build_host.leftovers, the census of what runs left
    running there, as its last line."""
    return shlex.join(["python3", "-c", Path(build_host.__file__).read_text(), json.dumps({"leftovers": ROOT})])


def read_census(found):
    """The census that a finished command's output ends with, and None; or None and why it is missing."""
    try:
        census = json.loads(last_line(found.stdout))
    except (TypeError, ValueError):
        return None, f"exit {found.returncode}: {last_line(found.stderr) or 'no output'}"
    if not isinstance(census, dict) or not isinstance(census.get("leftovers"), list):
        return None, census.get("error") if isinstance(census, dict) else f"it answered {census!r}"
    return census, None


def leftover_census(host):
    """The build host's census of leftover processes, read in its login shell as a run there reads
    it, and None; or None and why it is missing. A build host that cannot be reached raises; any
    other failure of the census is only reported, since it stops nothing."""
    try:
        found = ssh(host, census_command(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=CENSUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"it did not answer in {CENSUS_TIMEOUT}s"
    except OSError as error:
        raise unreachable(host, str(error)) from None
    if found.returncode == 255: raise unreachable(host, last_line(found.stderr))
    return read_census(found)


def leftover_lines(host, census, missing):
    """What `eng remote` prints of the census before it sends work: nothing when no process is left over."""
    if census is None: return [f"could not list the processes left over from runs on {host}: {missing}"]
    lines = [f"on {host}, {item}" for item in census.get("unchecked", [])]
    rows = census["leftovers"]
    if rows:
        lines.append(f"{host} runs {len(rows)} process{'es' if len(rows) > 1 else ''} left over from runs, which no "
                     f"live run or admission there accounts for. Nothing was stopped; tell the owner ({LEFTOVER_GUIDE}):")
    for row in rows:
        written = row.get("written")
        rate = f", {disk.human(written / row['seconds'])}/s since it started" if written and row.get("seconds") else ""
        where = {"checkout": "a checkout", "temporary directory": "the temporary directory"}.get(
            row["place"], f"simulator {row.get('simulator') or 'with no device named'}")
        lines += [f"  PID {row['pid']}, parent {row['parent']}, started {row['start']}, has written "
                  f"{'an unknown amount' if written is None else disk.human(written)}{rate}, from {where}: {row['path']}",
                  f"    {row['command']}"]
        descendants = row.get("descendants") or []
        if descendants:
            most = descendants[0]
            total = sum(descendant["written"] or 0 for descendant in descendants)
            amount = "" if most["written"] is None else f" ({disk.human(most['written'])})"
            lines.append(f"    and {len(descendants)} descendant{'s' if len(descendants) > 1 else ''}, which have written "
                         f"{disk.human(total)}, the most PID {most['pid']}{amount}: {most['command']}")
    return lines


def compare_hosts():
    """What `eng doctor` reports of the build host: this Mac's and the build host's default Xcodes
    (`xcode-select -p`), the Xcode at this Mac's developer directory, which every run there uses,
    and the simulator runtimes it lists. A difference, or a build host that cannot be reached, is
    an error; what could not be compared is named in unchecked. The processes there that runs left
    running are listed in leftovers, which are no error: they say what to tell the owner."""
    host = configured()
    report = {"host": host, "errors": [], "unchecked": []}
    if host is None:
        report["unchecked"].append(f"no build host is configured (VOCARO_BUILD_HOST, or the ssh alias {DEFAULT_HOST} "
                                   "in ~/.ssh/config), so this Mac was compared with none")
        return report
    developer = developer_directory()
    here = report["local"] = build_host.toolchain(developer)
    try:
        there = report["remote"] = survey(host, developer)
    except Misconfigured as problem:
        report["errors"].append(str(problem))
        report["unchecked"].append(f"{host}'s default Xcode, its Xcode at this Mac's developer directory and its "
                                   "simulator runtimes, which it did not report")
        return report
    try:
        census, missing = leftover_census(host)
    except Misconfigured as problem:
        census, missing = None, problem.problem
    if census is None:
        report["unchecked"].append(f"the processes left over from runs on {host}, which it did not list: {missing}")
    else:
        report["leftovers"] = census["leftovers"]
        report["unchecked"] += [f"on {host}, {item}" for item in census.get("unchecked", [])]
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    default = lambda side: (side.get("defaultDeveloperDir"), side.get("defaultXcode"))
    # When both Macs' default is this Mac's developer directory, the next check covers its version.
    shared = here.get("defaultDeveloperDir") == there.get("defaultDeveloperDir") == developer
    if default(here) != default(there) and not shared:
        report["errors"].append(
            f"the default Xcodes differ: `xcode-select -p` is {here.get('defaultDeveloperDir')} "
            f"({version(here.get('defaultXcode'))}) on this Mac and {there.get('defaultDeveloperDir')} "
            f"({version(there.get('defaultXcode'))}) on {host}, so whatever reads {host}'s default, such as "
            f"`ssh {host} xcodebuild -version` or a script without DEVELOPER_DIR, sees another Xcode than this Mac's; "
            "to fix it, the owner chooses the Xcode both Macs use and selects it with `sudo xcode-select -s PATH` "
            "on the Mac that differs")
    if developer is None or not here.get("xcode"):
        report["unchecked"].append("the Xcode at this Mac's developer directory and its simulator runtimes, since "
                                   "this Mac has no Xcode selected")
        return report
    if here["xcode"] != there.get("xcode"):
        report["errors"].append(
            f"the Xcode at this Mac's developer directory, {developer}, is {version(here['xcode'])} on this Mac and "
            f"{version(there.get('xcode')) if there.get('xcode') else 'missing'} on {host}, so eng remote refuses "
            f"every run there; to fix it, install this Mac's Xcode at that path on {host} (guides/shared-host.md, "
            "Setting up a build host), or select with DEVELOPER_DIR an Xcode both Macs have at the same path")
    if None in (here.get("runtimes"), there.get("runtimes")):
        report["unchecked"].append(f"the simulator runtimes, which simctl did not list on "
                                   f"{'this Mac' if here.get('runtimes') is None else host}")
    elif here["runtimes"] != there["runtimes"]:
        differences = runtime_differences(here["runtimes"], there["runtimes"], host)
        report["errors"].append(f"the simulator runtimes of the Xcode at {developer} differ: {'; '.join(differences)}; "
                                "to fix it, run `python3 scripts/eng remote setup --runtimes`, which copies each runtime "
                                "to the Mac that lacks it and deletes none (guides/shared-host.md, Setting up a build "
                                "host)")
    return report
