"""Run a lane or an admitted command on the build host: a second Mac reached over SSH.

Nothing here takes a slot on this host. The exact commit, and the toolkit commit that runs it,
go to bare repositories on the build host by git over SSH, never through GitHub. A working tree
with uncommitted changes goes as a snapshot commit on HEAD, which no branch holds. build_host.py,
sent along and run there by the host's own Python, checks them out in a persistent checkout,
so builds stay warm, and runs `eng run` or `eng admit` under the build host's own heavy-work
admission: that host's queue, not a private pool beside this one. The log and the result come
back to this checkout's .build/engineering/remote. A whole lane's result at a commit is also kept
by commit in the repository's Git directory, where every worktree of the repository can find it;
a snapshot's is not.

`eng run` sends a lane marked buildHost here first: when the build host declines it before
anything runs, because its queue or every checkout is busy, the lane runs on this Mac instead.
A configuration problem, such as a build host that cannot be reached or has another Xcode, stops
the lane for the owner's decision (agents/core.md, Shared resources); only `--here` then runs it
here. `eng doctor` compares the two Macs.
"""
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

from . import build_host, processes
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


def execute(root, config, host, work, *, lane=None, arguments=(), expect=None, decline=False):
    """Run eng's work on the build host and return its exit status, with the log and receipt kept here.

    work is the build host's eng arguments: run [--expect N] LANE [-- ARGUMENTS] or
    admit [--exclusive] -- COMMAND. It runs at HEAD, or at a snapshot commit of the working tree
    when that has uncommitted changes. With decline, the build host refuses work it cannot start
    now rather than waiting for it, and Unavailable says why. Misconfigured says what of the build
    host's configuration kept the work from running there, and what fixing it takes.
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
    plan = {"root": ROOT, "checkouts": CHECKOUTS, "project": project, "toolkit": kit, "work": work,
            "decline": decline, "busy": BUSY,
            "provision": "provision" in config.get("lanes", {}) and lane != "provision",
            "developerDir": developer, "xcode": local["xcode"],
            "log": f".build/engineering/{lane}/run.log" if lane is not None else None,
            "receipt": f".build/engineering/{name}/source.json"}

    repositories = [f"{ROOT}/{part['repository']}" for part in (project, kit) if part is not None]
    prepare = " && ".join([f"mkdir -p {shlex.quote(ROOT + '/repositories')}", *(
        f"{{ [ -d {shlex.quote(path)} ] || git init --bare --quiet {shlex.quote(path)}; }}" for path in repositories)])
    at = commit[:12] if commit == head else f"{commit[:12]}, a snapshot of the working tree on {head[:12]},"
    print(f"remote: sending {at} to {host}", file=sys.stderr, flush=True)
    prepared = ssh(host, prepare, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    # ssh's own failures, a name that does not resolve among them, exit 255.
    if prepared.returncode == 255: raise unreachable(host, last_line(prepared.stderr))
    if prepared.returncode:
        raise Misconfigured(f"{host} could not prepare ~/{ROOT} (exit {prepared.returncode}): "
                            f"{last_line(prepared.stderr) or 'no output'}",
                            f"make ~/{ROOT} writable there and install the Xcode this Mac uses, with its license "
                            "accepted and its first launch run (guides/shared-host.md, Setting up a build host)")
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
            framed = framed or kind in (b"L ", b"R ", b"O ")
            if kind == b"L ":
                log.write(data)
            elif kind == b"R ":
                result = json.loads(data)
            else:
                data = data if kind == b"O " else line
                if kind != b"O ": stray = last_line(line.decode(errors="replace")) or stray
                output.write(data)
                write(sys.stdout, data)
        channel.wait()
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
               "expect": expect, "work": work, "local": local, "result": result, "dispatched": started}
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
    if args.operation == "setup": return setup(host, args.formulae, args.imports or [], args.check)
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
    if not command: raise ValueError("Name the command: eng remote admit [--exclusive] -- COMMAND")
    with processes.forwarding_signals():
        return execute(root, config, host, ["admit", *(["--exclusive"] if args.exclusive else []), "--", *command])


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


def setup(host, formulae, modules, check):
    """Install what the gates use on the build host, then check it as `eng remote` will see it.
    With check, only check."""
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


def survey(host, developer):
    """The build host's build_host.toolchain, read in its login shell as a run there reads it."""
    request = shlex.join(["python3", "-c", Path(build_host.__file__).read_text(), json.dumps({"toolchain": developer})])
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


def compare_hosts():
    """What `eng doctor` reports of the build host: this Mac's and the build host's default Xcodes
    (`xcode-select -p`), the Xcode at this Mac's developer directory, which every run there uses,
    and the simulator runtimes it lists. A difference, or a build host that cannot be reached, is
    an error; what could not be compared is named in unchecked."""
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
        only = lambda side, other: [runtime for runtime in side["runtimes"] if runtime not in other["runtimes"]]
        differences = [f"only {name} has {', '.join(runtimes)}" for name, runtimes in
                       [("this Mac", only(here, there)), (host, only(there, here))] if runtimes]
        report["errors"].append(f"the simulator runtimes of the Xcode at {developer} differ: {'; '.join(differences)}; "
                                "to fix it, install the same runtimes on both Macs "
                                "(`xcodebuild -downloadPlatform PLATFORM` with that Xcode)")
    return report
