"""A lane's "sandbox": its command run under macOS Seatbelt (sandbox-exec) with a profile made for the run, which lets
it write only to the checkout it runs in, its run's directory, the temporary and cache folders and the paths the lane
names, and reach the network only as the lane says.

The profile is made on the Mac that runs the lane, once its admission has placed it and the tree it reads is known
(cli.admitted_tree), so a lane another Mac runs gets that Mac's paths. The admission is held outside the sandbox by
`qk run`, and sandbox-exec starts the command in its own place, so the process qk supervises, its timeout and its
kill are those of an unsandboxed lane.

The profile's shape is adapted from sluice's (boomdrivedev/sluice, crates/sluice/src/profiles/checks.rs, MIT; see
THIRD_PARTY_NOTICES.md): everything a user may do, but writes only where allowed, and the network only as said."""
import os
import pwd
import re
import subprocess
from pathlib import Path

from . import extensions

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Set in the environment of a sandboxed command, to the lane's name. A sandbox cannot be applied inside another
# (sandbox_apply fails with EPERM), so a lane run inside one runs under the one it is in.
INSIDE = "QUEUEKEEPER_SANDBOX"
PROFILE = "sandbox.sb"
# The writable devices: null and terminal, and /dev/ptmx, from which a command opens a pseudo-terminal of its own.
DEVICES = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/ptmx") ' \
          '(regex #"^/dev/fd/") (regex #"^/dev/ttys")'
PORT = re.compile(r"(localhost|\*):([1-9][0-9]{0,4})")
KEYS = frozenset({"write", "network"})


def settings(name, lane):
    """The lane's sandbox as {"write": [...], "network": [...]}, or None for a lane without one; ValueError, naming the
    lane, for a value it does not take."""
    value = lane.get("sandbox")
    if value is None or value is False: return None
    if value is True: value = {}
    if not isinstance(value, dict) or set(value) - KEYS:
        raise ValueError(f"{name}.sandbox must be true or an object of write and network")
    write = value.get("write", [])
    if not isinstance(write, list) or any(not isinstance(path, str) or not path.strip() for path in write):
        raise ValueError(f"{name}.sandbox.write must list paths: absolute, ~/..., or relative to the checkout")
    network = value.get("network", [])
    if not isinstance(network, list) or any(not isinstance(entry, str) for entry in network):
        raise ValueError(f"{name}.sandbox.network must list endpoints")
    for entry in network:
        if entry in ("*", "localhost") or PORT.fullmatch(entry) and int(entry.rpartition(":")[2]) <= 65535: continue
        if entry.startswith("/"): continue
        raise ValueError(f"{name}.sandbox.network: {entry!r} is none of localhost, localhost:PORT, *:PORT, * or a "
                         "socket's absolute path; Seatbelt tells only localhost from any other host, so a host is "
                         "named by its port, *:443")
    return {"write": list(write), "network": list(network)}


def check(name, lane):
    """Refuse a sandbox the lane cannot run under (config.parse): a self lane admits its parts, which may go to other
    Macs over SSH and run nested lanes; an extension refuses one for the lanes it knows cannot run in one
    (extensions.SANDBOX_REFUSALS)."""
    if settings(name, lane) is None: return
    if lane.get("resource") == "self":
        raise ValueError(f"{name}.sandbox needs an admitted lane: a self lane's script admits its parts, which may "
                         "run on other Macs, outside any sandbox")
    for refusal in extensions.SANDBOX_REFUSALS:
        why = refusal(lane)
        if why: raise ValueError(f"{name}.sandbox cannot be: {why}")


def darwin_folder(name):
    """getconf's DARWIN_USER_TEMP_DIR or DARWIN_USER_CACHE_DIR, or None."""
    try:
        found = subprocess.run(["/usr/bin/getconf", name], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return found if found.startswith("/") else None


def git_folder(tree):
    """The git directory of the checkout tree, which a linked worktree keeps outside it, or None."""
    try:
        found = subprocess.run(["git", "-C", str(tree), "rev-parse", "--absolute-git-dir"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def writable(tree, directory, declared, environment=None):
    """The folders the command may write to, resolved, each once, in order: the tree, its git directory, the run's
    directory, the temporary and cache folders, then the lane's own, a relative one from the tree."""
    environment = os.environ if environment is None else environment
    places = [tree, git_folder(tree), directory, "/private/tmp", "/private/var/tmp",
              darwin_folder("DARWIN_USER_TEMP_DIR"), darwin_folder("DARWIN_USER_CACHE_DIR"), environment.get("TMPDIR"),
              home() / "Library/Caches", home() / ".cache"]
    for path in declared:
        path = Path(path).expanduser()
        places.append(path if path.is_absolute() else Path(tree) / path)
    found = []
    for place in places:
        if not place: continue
        resolved = os.path.realpath(place)
        if resolved not in found: found.append(resolved)
    return found


def quoted(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(write, network, own_sockets):
    """The Seatbelt profile's text: writes only to write and the devices, and the network as network says; unix
    sockets in own_sockets, the run's own folders, whatever it says, so that a test's server and client may talk."""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write*", *(f"  (subpath {quoted(path)})" for path in write), f"  {DEVICES})",
             # Every program's loader writes to dtrace's helper and goes on when refused; no check needs it.
             '(deny file-write-data (literal "/dev/dtracehelper") (with no-report))']
    if "*" not in network:
        lines.append("(deny network*)")
        for path in own_sockets:
            lines += [f"(allow network-bind network-inbound network-outbound (local unix-socket (subpath {quoted(path)})))",
                      f"(allow network-outbound (remote unix-socket (subpath {quoted(path)})))"]
        resolves = False
        for entry in network:
            if entry == "localhost":
                lines += ['(allow network-bind network-inbound (local ip "localhost:*"))',
                          '(allow network-outbound (remote ip "localhost:*"))']
            elif entry.startswith("/"):
                lines.append(f"(allow network-outbound (remote unix-socket (path-literal {quoted(os.path.realpath(entry))})))")
            else:
                host, _, port = entry.rpartition(":")
                if host == "localhost":
                    lines += [f'(allow network-bind network-inbound (local ip "localhost:{port}"))',
                              f'(allow network-outbound (remote ip "localhost:{port}"))']
                else:
                    lines.append(f'(allow network-outbound (remote tcp "*:{port}"))')
                    resolves = True
        # A host named by its name is found through mDNSResponder's socket.
        if resolves:
            lines.append('(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))')
    return "\n".join(lines) + "\n"


class Unusable(RuntimeError):
    """The profile could not be written or applied on this Mac: the lane does not start."""


def prepared(name, lane, tree, directory, file=PROFILE, environment=None):
    """(the words that start the lane's command under its sandbox, what its receipt records of it) once its profile is
    written to directory/file and proved to apply here; ([], None) for a lane without one. A lane run inside a sandbox
    already runs under that one, which cannot be nested. Unusable says why it cannot run. The words set INSIDE in the
    command's environment from within the sandbox, so that a lane group's members are told so as a lane is."""
    environment = os.environ if environment is None else environment
    said = settings(name, lane)
    if said is None: return [], None
    if environment.get(INSIDE): return [], {"inside": environment[INSIDE]}
    write = writable(tree, directory, said["write"], environment)
    own = [path for path in write if path not in ("/private/tmp", "/private/var/tmp")]
    path = Path(directory) / file
    try:
        path.write_text(profile(write, said["network"], own))
    except OSError as error:
        raise Unusable(f"its sandbox's profile could not be written to {path}: {error}") from None
    try:
        tried = subprocess.run([SANDBOX_EXEC, "-f", str(path), "/usr/bin/true"], capture_output=True, text=True,
                               timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unusable(f"{SANDBOX_EXEC} could not start: {error}") from None
    if tried.returncode != 0:
        why = (tried.stderr or tried.stdout).strip() or f"exit {tried.returncode}"
        raise Unusable(f"its sandbox's profile, {path}, does not apply here: {why}")
    return [SANDBOX_EXEC, "-f", str(path), "/usr/bin/env", f"{INSIDE}={name}"], \
        {"profile": str(path), "write": write, "network": said["network"] or "off"}
