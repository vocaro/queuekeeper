"""The build host's side of `eng remote`, sent over SSH and run there by the host's own Python.

It imports nothing from the toolkit, because it runs before the toolkit is checked out there,
and Python 3.9, a Mac's system Python, runs it. It claims a persistent checkout of the project,
places the dispatched commits, and runs `eng run` or `eng admit` there under the build host's
own heavy-work admission. Standard output carries framed lines: `O` for output, `L` for the
lane's log as it grows, and a closing `R` with the result as JSON. Standard input is the
dispatcher's lifeline: its end, which a stop, the dispatcher's death or a dropped connection
sends, stops the run as a stop signal stops a local one. Given `{"toolchain": DEVELOPER_DIR}`
instead of a plan, it prints the Mac's Xcodes and simulator runtimes for `eng doctor`; given
`{"images": ...}` or `{"receive": ...}`, it lists the Mac's runtime images or adds one read from
standard input, for `eng remote setup --runtimes`.
"""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

# Seconds between reports while every checkout is in use, and before waiting for one gives up.
REPORT_SECONDS, CLAIM_TIMEOUT = 300, 5400
# Seconds the output of a finished command may take to drain: a descendant can hold the pipe.
DRAIN_SECONDS = 5
# Bytes of a runtime image read or written at once.
CHUNK = 8 << 20
# The platform a runtime image names when simctl gives no runtime bundle to name it by.
PLATFORMS = {"com.apple.platform.iphonesimulator": "iOS", "com.apple.platform.appletvsimulator": "tvOS",
             "com.apple.platform.watchsimulator": "watchOS", "com.apple.platform.xrsimulator": "visionOS"}

_output = threading.Lock()
_stop = threading.Event()


class Refusal(RuntimeError):
    """The build host will not start the work, and nothing of it ran. A busy build host cannot start
    it now; any other refusal is its configuration's, and fix says what fixing that takes."""

    def __init__(self, reason, fix=None, busy=False):
        super().__init__(reason)
        self.fix, self.busy = fix, busy


def emit(kind, data):
    """Write one framed line. A closed connection is a stop: nobody is left to read the result."""
    if not data.endswith(b"\n"): data += b"\n"
    with _output:
        try:
            sys.stdout.buffer.write(kind + b" " + data)
            sys.stdout.buffer.flush()
        except OSError:
            _stop.set()


def say(text):
    emit(b"O", f"build-host: {text}".encode())


def output(command, environment=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def xcode(developer_dir):
    """`xcodebuild -version` of the Xcode at developer_dir, or None."""
    if developer_dir and Path(developer_dir).is_dir():
        return output(["xcrun", "xcodebuild", "-version"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    return None


def identity(developer_dir):
    """The Mac a result was measured on, its macOS and the Xcode at developer_dir."""
    number = lambda name: int(output(["sysctl", "-n", name]) or 0) or None
    return {"name": output(["scutil", "--get", "LocalHostName"]) or os.uname().nodename,
            "model": output(["sysctl", "-n", "hw.model"]), "cpus": number("hw.ncpu"),
            "memoryBytes": number("hw.memsize"), "macOS": output(["sw_vers", "-productVersion"]),
            "build": output(["sw_vers", "-buildVersion"]), "developerDir": developer_dir, "xcode": xcode(developer_dir)}


def toolchain(developer_dir):
    """The Mac's identity with the Xcode at developer_dir, its default Xcode (`xcode-select -p`) and
    the simulator runtimes the Xcode at developer_dir lists, which `eng doctor` compares."""
    found = identity(developer_dir)
    default = output(["xcode-select", "-p"])
    runtimes = None
    if found["xcode"]:
        listed = output(["xcrun", "simctl", "list", "runtimes", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
        try:
            runtimes = sorted(f"{runtime['name']} ({runtime['buildversion']})"
                              for runtime in json.loads(listed)["runtimes"] if runtime.get("isAvailable"))
        except (TypeError, ValueError, KeyError):
            pass
    return {**found, "defaultDeveloperDir": default, "defaultXcode": xcode(default), "runtimes": runtimes}


def image_file(record):
    """The file of the disk image that a record of `simctl runtime list` names, and None; or None and
    why no such file can be read. simctl names a disk image by its own path, and a cryptex by its
    asset's AssetData folder, whose Restore folder holds the image beside the cryptex's signatures.
    Either file's size is the record's sizeBytes (Xcode 27.2 on macOS 27.0.1 and 27.2, October 2026)."""
    path, size = record.get("path"), record.get("sizeBytes")
    if not path or not isinstance(size, int):
        return None, "simctl names no path and size of its image"
    path = Path(path)
    try:
        candidates = [path] if path.is_file() else sorted((path / "Restore").glob("*.dmg"))
        matching = [candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_size == size]
    except OSError as error:
        return None, f"{path} cannot be read: {error}"
    if len(matching) != 1:
        return None, f"{path} holds no single disk image of its {size} bytes"
    if not os.access(matching[0], os.R_OK):
        return None, f"{matching[0]} cannot be read"
    return str(matching[0]), None


def images(developer_dir):
    """The runtime disk images `simctl runtime list` reports, each with the runtime it holds named as
    toolchain names it, NAME (BUILD), and its image file, or None when simctl lists none."""
    listed = output(["xcrun", "simctl", "runtime", "list", "-j"], {**os.environ, "DEVELOPER_DIR": developer_dir})
    try:
        records = list(json.loads(listed).values())
    except (TypeError, ValueError, AttributeError):
        return None
    found = []
    for record in records:
        if not isinstance(record, dict) or not record.get("build"): continue
        bundle = Path(record.get("runtimeBundlePath") or "")
        name = bundle.stem if bundle.suffix == ".simruntime" else \
            f"{PLATFORMS.get(record.get('platformIdentifier'), record.get('platformIdentifier'))} {record.get('version')}"
        file, unreadable = image_file(record)
        found.append({"runtime": f"{name} ({record['build']})", "name": name, "build": record["build"],
                      "identifier": record.get("identifier"), "state": record.get("state"),
                      "sizeBytes": record.get("sizeBytes"), "architectures": record.get("supportedArchitectures") or [],
                      "file": file, "unreadable": unreadable})
    return sorted(found, key=lambda image: image["runtime"])


def stage(directory, clear):
    """The free bytes where runtime images are staged, and the staged images an earlier copy left
    there, which clear removes. Only clear creates the directory."""
    directory = Path(directory)
    if clear: directory.mkdir(parents=True, exist_ok=True)
    left = sorted(path.name for path in directory.glob("*.dmg*")) if directory.is_dir() else []
    if clear:
        for name in left: (directory / name).unlink(missing_ok=True)
    existing = next(path for path in (directory, *directory.parents) if path.exists())
    return {"free": shutil.disk_usage(existing).free, "left": left}


def receive(stream, directory, build, size, developer_dir, progress=None):
    """Stage a runtime image read from stream in directory, then have simctl add it, moving it into
    simctl's own storage. Only an image of exactly size bytes is added, and whatever fails removes
    the staged file, so a failed copy adds nothing and leaves nothing behind. Return None once the
    image is added, else why it was not. progress, given the bytes received so far, may raise to stop."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    partial, staged = directory / f"{build}.dmg.partial", directory / f"{build}.dmg"
    try:
        received = 0
        with partial.open("wb") as file:
            while not _stop.is_set():
                chunk = stream.read(CHUNK)
                if not chunk: break
                file.write(chunk)
                received += len(chunk)
                if progress is not None: progress(received)
        if _stop.is_set(): return "stopped before it was added"
        if received != size: return f"received {received} of its {size} bytes"
        partial.rename(staged)
        # In a session of its own, so a terminal's interrupt cannot end it halfway through.
        added = subprocess.run(["xcrun", "simctl", "runtime", "add", str(staged), "--move"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, env={**os.environ, "DEVELOPER_DIR": developer_dir},
                               start_new_session=True)
        if added.returncode:
            lines = [line.strip() for line in (added.stderr + added.stdout).splitlines() if line.strip()]
            return f"simctl runtime add exited {added.returncode}: {lines[-1] if lines else 'no output'}"
        return None
    except OSError as error:
        return f"staging it in {directory} failed: {error}"
    finally:
        # simctl removes the staged file once it holds the image; a failure leaves it to remove.
        for path in (partial, staged): path.unlink(missing_ok=True)


def answer(request):
    """build_host.py's answer to a request other than a plan, for `eng doctor` and `eng remote setup`."""
    if "toolchain" in request: return toolchain(request["toolchain"])
    if "images" in request:
        return {"images": images(request["images"]), **stage(Path.home() / request["staging"], request.get("clear"))}
    # A stop or a dropped connection ends the copy, and the staged file is removed.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    copy = request["receive"]
    return {"failed": receive(sys.stdin.buffer, Path.home() / copy["staging"], copy["build"], copy["size"],
                              copy["developerDir"])}


def git(*arguments, cwd=None):
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def changes(checkout):
    """Tracked changes and nonignored untracked files, which a run left in a checkout."""
    return git("status", "--porcelain", "--untracked-files=normal", cwd=checkout) if checkout.exists() else ""


def claim(base, count, directory, wait=True):
    """Lock a free checkout slot without changes, waiting while every one is in use, if wait.

    The lock is an flock the run's command inherits, so it holds while that command lives. A
    checkout with changes is left as it is, for inspection, and passed over.
    """
    base.mkdir(parents=True, exist_ok=True)
    started, reported, named = time.monotonic(), None, set()
    while not _stop.is_set():
        changed = 0
        for index in range(count):
            descriptor = os.open(base / f"{index}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            checkout = base / str(index) / directory
            left = changes(checkout)
            if not left:
                return index, descriptor
            os.close(descriptor)
            changed += 1
            if index not in named:
                say(f"passing over {checkout}, which a run left with changes:\n{left}")
                named.add(index)
        if changed == count:
            repository = base.parents[1] / "repositories" / f"{directory}.git"
            raise Refusal(f"every checkout under {base} has changes a run left",
                          f"inspect and remove them, each with `git --git-dir {repository} worktree remove --force PATH`")
        if not wait:
            raise Refusal(f"every checkout of {directory} is in use", busy=True)
        waited = int(time.monotonic() - started)
        if waited >= CLAIM_TIMEOUT:
            raise Refusal(f"no checkout of {directory} came free in {CLAIM_TIMEOUT}s", busy=True)
        if reported is None or waited - reported >= REPORT_SECONDS:
            say(f"waiting ({waited}s): every checkout of {directory} is in use")
            reported = waited
        time.sleep(1)
    return None


def place(repository, checkout, commit, ref, origin=None):
    """Check commit out, detached, in a persistent worktree whose ignored inputs and builds stay.

    Only the toolkit names an origin: a heavy lane's policy refresh fetches it."""
    if origin and output(["git", "--git-dir", str(repository), "config", "--get", "remote.origin.url"]) != origin:
        git("--git-dir", str(repository), "config", "remote.origin.url", origin)
    if checkout.exists():
        git("checkout", "--detach", "--quiet", commit, cwd=checkout)
    else:
        git("--git-dir", str(repository), "worktree", "prune")
        git("--git-dir", str(repository), "worktree", "add", "--detach", "--quiet", str(checkout), commit)
    if git("rev-parse", "HEAD", cwd=checkout) != commit:
        raise RuntimeError(f"{checkout} is not at {commit}")
    # The detached worktree keeps the commit reachable; the pushed ref only carried it here.
    subprocess.run(["git", "--git-dir", str(repository), "update-ref", "-d", ref], capture_output=True)


def credentials(environment, path):
    """Give the run's git the build host's credential file and no other helper.

    SwiftPM clones private dependencies with git, and an SSH session cannot unlock the keychain
    that osxkeychain, Xcode's git default, reads. The variables reach only this run, so the
    owner's own git configuration on the build host is unchanged."""
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    for offset, value in enumerate(["", f"store --file={shlex.quote(str(path))}"]):
        environment[f"GIT_CONFIG_KEY_{count + offset}"] = "credential.helper"
        environment[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    environment["GIT_CONFIG_COUNT"] = str(count + 2)


def relay(stream):
    for line in iter(stream.readline, b""):
        emit(b"O", line)


def follow(path, done):
    """Send the lane's log from its start as it grows. The lane creates it afresh."""
    stream, pending = None, b""
    try:
        while True:
            finished = done.is_set()
            if stream is None:
                try: stream = path.open("rb")
                except FileNotFoundError: pass
            if stream is not None:
                *lines, pending = (pending + stream.read()).split(b"\n")
                for line in lines: emit(b"L", line)
            if finished: break
            time.sleep(0.2)
    finally:
        if stream is not None: stream.close()
    if pending: emit(b"L", pending)


def step(command, checkout, environment, lock, log):
    """Run one eng command; a stop sends it SIGTERM, which it carries out as a supervised stop."""
    if log is not None: log.unlink(missing_ok=True)
    child = subprocess.Popen(command, cwd=checkout, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock,))
    done = threading.Event()
    threads = [threading.Thread(target=relay, args=(child.stdout,), daemon=True)]
    if log is not None: threads.append(threading.Thread(target=follow, args=(log, done), daemon=True))
    for thread in threads: thread.start()
    forwarded = False
    while child.poll() is None:
        if _stop.is_set() and not forwarded:
            say("stop requested; sending SIGTERM to the run, which stops its command and leaves admission")
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            forwarded = True
        time.sleep(0.1)
    done.set()
    for thread in threads: thread.join(DRAIN_SECONDS)
    return child.returncode


def watch():
    """The end of standard input is the dispatcher's stop.

    It reads the descriptor, not sys.stdin: Python 3.9 aborts at exit when a daemon thread is
    blocked holding the buffered reader's lock (observed on macOS 27.2, October 2026)."""
    try:
        while os.read(0, 65536): pass
    except OSError:
        pass
    _stop.set()


def main(plan):
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: _stop.set())
    threading.Thread(target=watch, daemon=True).start()
    result = {"started": time.time(), "exitStatus": None, "stopped": False,
              "host": identity(plan.get("developerDir"))}
    # Until the first command starts, a failure is a refusal: nothing ran. The dispatcher may run
    # the work on its own Mac instead only when the refusal is busy.
    begun = False
    try:
        expected, found = plan.get("xcode"), result["host"]["xcode"]
        if expected and found != expected:
            version = lambda text: " ".join(text.split())
            raise Refusal(f"the Xcode at {plan['developerDir']} is {version(found or 'missing')}, not the dispatcher's "
                          f"{version(expected)}", "install this Mac's Xcode at that path on the build host "
                          "(guides/shared-host.md, Setting up a build host), or select with DEVELOPER_DIR an Xcode "
                          "both Macs have at the same path")
        root, project, toolkit = Path.home() / plan["root"], plan["project"], plan.get("toolkit")
        base = root / "checkouts" / project["directory"]
        claimed = claim(base, plan["checkouts"], project["directory"], wait=not plan.get("decline"))
        if claimed is None:
            result["stopped"] = True
            return result
        index, lock = claimed
        checkout = base / str(index) / project["directory"]
        place(root / project["repository"], checkout, project["commit"], project["ref"], project.get("origin"))
        kit = checkout
        if toolkit is not None:
            kit = base / str(index) / toolkit["directory"]
            place(root / toolkit["repository"], kit, toolkit["commit"], toolkit["ref"], toolkit.get("origin"))
        result.update(checkout=str(checkout), slot=index)
        say(f"{checkout} at {project['commit'][:12]}")
        environment = {**os.environ, "VOCARO_ENGINEERING_ROOT": str(kit)}
        if plan.get("developerDir"): environment["DEVELOPER_DIR"] = plan["developerDir"]
        if (root / "git-credentials").is_file(): credentials(environment, root / "git-credentials")
        eng = [sys.executable, str(kit / "bin/eng"), "--project", str(checkout)]
        steps = [("provision", [*eng, "run", "provision"], checkout / ".build/engineering/provision/run.log")] \
            if plan.get("provision") else []
        steps.append(("work", [*eng, *plan["work"]], checkout / plan["log"] if plan.get("log") else None))
        for stage, command, log in steps:
            if _stop.is_set(): break
            begun = True
            result.update(stage=stage, exitStatus=step(command, checkout, environment, lock, log))
            if result["exitStatus"] != 0: break
        result["stopped"] = _stop.is_set()
        receipt = checkout / plan["receipt"]
        # A command that failed before it wrote a receipt leaves the previous run's in place.
        fresh = receipt.is_file() and receipt.stat().st_mtime >= result["started"]
        if fresh:
            result["receipt"] = json.loads(receipt.read_text())
        # `eng run --if-free` declined the lane before running it, and wrote no receipt.
        if plan.get("decline") and result.get("stage") == "work" and result["exitStatus"] == plan["busy"] \
                and not fresh:
            result.update(refused="its heavy-work queue cannot take the lane now", busy=True)
    except Refusal as refusal:
        result.update(refused=str(refusal), busy=refusal.busy, fix=refusal.fix)
    except (OSError, RuntimeError, ValueError) as error:
        result["error" if begun else "refused"] = str(error)
    finally:
        result["finished"] = time.time()
        emit(b"R", json.dumps(result).encode())
    return result


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    if {"toolchain", "images", "receive"} & request.keys(): print(json.dumps(answer(request)))
    else: main(request)
