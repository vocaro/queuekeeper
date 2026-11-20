"""A lane's "sandbox": its command run under macOS Seatbelt (sandbox-exec) with a profile made for the run, which lets
it write only to the checkout it runs in, its run's directory, the temporary and cache folders and the paths the lane
names, and reach the network only as the lane says.

The profile is made on the Mac that runs the lane, once its admission has placed it and the tree it reads is known
(cli.admitted_tree), so a lane another Mac runs gets that Mac's paths. The admission is held outside the sandbox by
`qk run`, and sandbox-exec starts the command in its own place, so the process qk supervises, its timeout and its
kill are those of an unsandboxed lane.

The profile's shape is adapted from sluice's (boomdrivedev/sluice, crates/sluice/src/profiles/checks.rs, MIT; see
THIRD_PARTY_NOTICES.md): everything a user may do, but writes only where allowed, and the network only as said."""
import json
import math
import os
import pwd
import re
import contextlib
import contextvars
import ipaddress
import secrets
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import plugins

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Set in the environment of a sandboxed command, to the lane's name. A sandbox cannot be applied inside another
# (sandbox_apply fails with EPERM), so a lane run inside one runs under the one it is in.
INSIDE = "QUEUEKEEPER_SANDBOX"
PROFILE = "sandbox.sb"
# The writable devices: null and terminal, and /dev/ptmx, from which a command opens a pseudo-terminal of its own.
DEVICES = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/ptmx") ' \
          '(regex #"^/dev/fd/") (regex #"^/dev/ttys")'
# What macOS's log reads of the kernel's refusals. It keeps them only for a reader streaming at the time, not in the
# store `log show` reads (measured on macOS 27.2), so a run's are read as they happen (Refusals).
LOG = "/usr/bin/log"
# Each refusal the kernel logs, "Sandbox: PROCESS(PID) deny(1) OPERATION TARGET", may come as "N duplicate reports for"
# it, and ends with the deny rule's message, the mark of the run and lane, on a line of its own.
REFUSAL = re.compile(r"(?:\d+ duplicate reports? for )?Sandbox: (.+?)\(\d+\) deny\(\d+\) (\S+)(?: (.*))?")
# How long the stream may take to start before the command starts without it, and how long refusals the command's
# last moments caused may take to arrive once it has ended.
STREAM_READY = 5
STREAM_SETTLE = 0.5
# The refusals a receipt keeps, and those a summary names.
KEPT = 50
SHOWN = 5
# What a program prints when the sandbox it applies is refused, as macOS refuses one inside another (measured on
# macOS 27.2: sandbox-exec and SwiftPM's manifest step print it, and the command fails).
NESTED = re.compile(r"sandbox_apply: Operation not permitted")
# The variables that tell a command's HTTP clients the proxy a lane's named hosts are reached through, in the cases
# clients read them in.
PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
# How long the proxy waits for a request's head, and to connect to the host it names.
PROXY_TIMEOUT = 30
# The longest request head the proxy reads.
PROXY_HEAD = 65536
# What a refused write or connection makes most programs print, without saying what was refused.
UNPERMITTED = re.compile(r"Operation not permitted|EPERM\b")
PORT = re.compile(r"(localhost|\*):([1-9][0-9]{0,4})")
# A host the lane names, reached through its own filtering proxy (Proxy): a name with a dot, or *. and a domain for any
# name under it, and a port.
HOST = re.compile(r"((?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                  r"[A-Za-z0-9])?)+):([1-9][0-9]{0,4})")
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
        if HOST.fullmatch(entry) and int(entry.rpartition(":")[2]) <= 65535 and not is_address(entry.rpartition(":")[0]):
            continue
        raise ValueError(f"{name}.sandbox.network: {entry!r} is none of localhost, localhost:PORT, HOST:PORT, "
                         "*.DOMAIN:PORT, *:PORT, * or a socket's absolute path")
    if hosts(network) and any(entry == "*" or entry.startswith("*:") for entry in network):
        raise ValueError(f"{name}.sandbox.network names hosts and also any host, which would let a connection past "
                         "the hosts' proxy: name hosts or *:PORT, not both")
    return {"write": list(write), "network": list(network)}


def is_address(host):
    try: ipaddress.ip_address(host)
    except ValueError: return False
    return True


def hosts(network):
    """The entries of network that name hosts, which a filtering proxy admits (Proxy): [(host, port)], host lower case,
    and *.DOMAIN for any name under DOMAIN."""
    found = []
    for entry in network:
        match = HOST.fullmatch(entry) if entry != "localhost" and not entry.startswith("localhost:") else None
        if match: found.append((match.group(1).lower(), int(match.group(2))))
    return found


def check(name, lane):
    """Refuse a sandbox the lane cannot run under (config.parse): a self lane admits its parts, which may go to other
    Macs over SSH and run nested lanes; a plugin refuses one for the lanes it knows cannot run in one
    (plugins.SANDBOX_REFUSALS)."""
    if settings(name, lane) is None: return
    if lane.get("resource") == "self":
        raise ValueError(f"{name}.sandbox needs an admitted lane: a self lane's script admits its parts, which may "
                         "run on other Macs, outside any sandbox")
    for refusal in plugins.SANDBOX_REFUSALS:
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


def profile(write, network, own_sockets, mark=None, proxy=None):
    """The Seatbelt profile's text: writes only to write and the devices, and the network as network says; unix
    sockets in own_sockets, the run's own folders, whatever it says, so that a test's server and client may talk.
    mark, the run's and lane's, is the message of every refusal the kernel logs of it (Refusals). A host the network
    names is reached only through the proxy, on localhost's port proxy, which admits only those hosts (Proxy)."""
    marked = f" (with message {quoted(mark)})" if mark else ""
    lines = ["(version 1)", "(allow default)", f"(deny file-write*{marked})",
             "(allow file-write*", *(f"  (subpath {quoted(path)})" for path in write), f"  {DEVICES})",
             # Every program's loader writes to dtrace's helper and goes on when refused; no check needs it.
             '(deny file-write-data (literal "/dev/dtracehelper") (with no-report))']
    if "*" not in network:
        lines.append(f"(deny network*{marked})")
        for path in own_sockets:
            lines += [f"(allow network-bind network-inbound network-outbound (local unix-socket (subpath {quoted(path)})))",
                      f"(allow network-outbound (remote unix-socket (subpath {quoted(path)})))"]
        resolves = False
        if proxy is not None:
            lines.append(f'(allow network-outbound (remote ip "localhost:{proxy}"))')
        named = {f"{host}:{port}" for host, port in hosts(network)}
        for entry in network:
            if entry in named: continue
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


def prepared(name, lane, tree, directory, file=PROFILE, environment=None, mark=None, refusals=None):
    """(the words that start the lane's command under its sandbox, what its receipt records of it) once its profile is
    written to directory/file and proved to apply here; ([], None) for a lane without one. A lane run inside a sandbox
    already runs under that one, which cannot be nested. Unusable says why it cannot run. The words set INSIDE in the
    command's environment from within the sandbox, so that a lane group's members are told so as a lane is. mark, a
    Refusals' mark for the lane, tags the refusals the kernel logs of it. A lane that names hosts reaches them through
    a proxy of its own (Proxy), which refusals holds for the run, and its command is told the proxy's address in the
    proxy variables (PROXY_VARIABLES)."""
    environment = os.environ if environment is None else environment
    said = settings(name, lane)
    if said is None: return [], None
    if refusals is None and mark is None and CURRENT.get() is not None:
        refusals = CURRENT.get()
        mark = refusals.mark(name)
    if environment.get(INSIDE): return [], {"inside": environment[INSIDE]}
    write = writable(tree, directory, said["write"], environment)
    own = [path for path in write if path not in ("/private/tmp", "/private/var/tmp")]
    path = Path(directory) / file
    named = hosts(said["network"])
    proxy = None
    if named:
        if refusals is None: raise Unusable("its sandbox names hosts, whose proxy this run has no place for")
        try: proxy = refusals.proxy(name, named)
        except OSError as error: raise Unusable(f"its sandbox's proxy for the hosts it names could not start: {error}") from None
    try:
        applied(path, profile(write, said["network"], own, mark, proxy and proxy.port))
    except Unusable:
        if proxy is not None: proxy.close()
        raise
    address = [f"{variable}=http://127.0.0.1:{proxy.port}" for variable in PROXY_VARIABLES] + \
        ["NO_PROXY=localhost,127.0.0.1,::1", "no_proxy=localhost,127.0.0.1,::1"] if proxy else []
    return [SANDBOX_EXEC, "-f", str(path), "/usr/bin/env", f"{INSIDE}={name}", *address], \
        {"profile": str(path), "write": write, "network": said["network"] or "off",
         **({"proxy": f"127.0.0.1:{proxy.port}"} if proxy else {})}


def applied(path, text):
    """Write the profile text to path and prove it applies here, or raise Unusable."""
    try:
        path.write_text(text)
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


# The Refusals of the run a lane group's members are prepared in (watching), which prepared finds without being told.
CURRENT = contextvars.ContextVar("queuekeeper_sandbox_refusals", default=None)


@contextlib.contextmanager
def watching(refusals):
    """Read refusals, a Refusals or None, for the length of the block, as the run's own (CURRENT), so that each lane
    prepared in it tags its refusals and has its proxy there."""
    if refusals is None:
        yield None
        return
    token = CURRENT.set(refusals)
    try:
        with refusals: yield refusals
    finally:
        CURRENT.reset(token)


def observed_group(receipt, refusals, lanes):
    """observed for each member of a lane group with a sandbox, its log and receipt in the folder lanes."""
    for name, sandboxed in (receipt.get("sandbox") or {}).items():
        try: ended = json.loads((Path(lanes) / f"{name}.json").read_text()).get("status")
        except (OSError, ValueError, AttributeError): ended = None
        observed(sandboxed, refusals, name, Path(lanes) / f"{name}.log", ended)


class Refusals:
    """The writes and connections the kernel refused a run's sandboxed commands, read from macOS's log as they happen:
    a `log stream` of the kernel's messages that carry the run's mark, from before its command starts to just after
    it ends. Each lane's profile tags its deny rules with mark(lane) (profile), so that a group's refusals are each
    its lane's. A stream that cannot start costs the run nothing: it is run without, and nothing is reported. The
    stream ends with the run, and by its own --timeout should qk itself be killed (limit, seconds).

    It reads what the kernel reports, which is not certainly every refusal: measured on macOS 27.2, the kernel did not
    report about one in four refused writes that a Python process made as it started, and none it made 0.3 s in, with
    the stream proved to receive (probed). A failed run whose log says "Operation not permitted" and of which none was
    read is told so (observed)."""

    def __init__(self, directory, limit):
        self.path = Path(directory) / "sandbox-refusals.ndjson"
        self.token = f"queuekeeper-sandbox-{secrets.token_hex(6)}"
        self.limit = limit
        self.process = self.file = None
        self.ready = False
        self.proxies = {}

    def mark(self, lane):
        return f"{self.token}/{lane}"

    def proxy(self, lane, named):
        """A started Proxy admitting named, [(host, port)], for lane's commands, which this run ends (close)."""
        started = self.proxies[lane] = Proxy(named).start()
        return started

    def close(self):
        """End the run's proxies; what they refused stays readable."""
        for each in self.proxies.values(): each.close()

    def __enter__(self):
        minutes = max(1, math.ceil((self.limit or 3600) / 60)) + 5
        try:
            self.file = self.path.open("w")
            self.process = subprocess.Popen(
                [LOG, "stream", "--style", "ndjson", "--timeout", f"{minutes}m", "--predicate",
                 f'eventMessage CONTAINS "{self.token}"'],
                stdin=subprocess.DEVNULL, stdout=self.file, stderr=subprocess.DEVNULL)
        except OSError:
            self.process = None
            return self
        self.ready = self.probed("start")
        return self

    def probed(self, when):
        """Whether the stream receives the kernel's refusals, proved by one: a write refused under a profile of its
        own, tried again until the stream has its refusal or STREAM_READY seconds have passed. The stream says what it
        filters before it receives anything (up to two seconds before, measured on macOS 27.2), so the line it writes
        first proves nothing. At the end, the refusal of a probe made after the command has ended is delivered after
        the command's own."""
        mark = f"{self.token}:probe-{when}"
        rule = f"(version 1)(allow default)(deny file-write* (with message {quoted(mark)}))"
        deadline = time.monotonic() + STREAM_READY
        tries = 0
        while time.monotonic() < deadline and self.process.poll() is None:
            # A target of its own each time, which the kernel would otherwise count as a duplicate of the last.
            tries += 1
            try:
                subprocess.run([SANDBOX_EXEC, "-p", rule, "/usr/bin/touch", str(self.path.with_name(f"sandbox-probe-{when}-{tries}"))],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                return False
            waited = time.monotonic() + 0.25
            while time.monotonic() < waited:
                try:
                    if mark in self.path.read_text(errors="replace"): return True
                except OSError:
                    return False
                time.sleep(0.02)
        return False

    def __exit__(self, *failure):
        if self.process is not None:
            if self.process.poll() is None:
                # What the command's last moments caused, delivered by the time a refusal made after them is.
                if not (self.ready and self.probed("end")): time.sleep(STREAM_SETTLE)
                self.process.terminate()
            try: self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.file is not None: self.file.close()
        self.close()
        return False

    def refused(self, lane):
        """What the kernel refused lane's commands, each operation on each target once in the order first refused:
        [{"operation", "target", "process"}], at most KEPT."""
        found = {}
        try: lines = self.path.read_text(errors="replace").splitlines()
        except OSError: return []
        for line in lines:
            try: message = json.loads(line).get("eventMessage")
            except (ValueError, AttributeError): continue
            if not isinstance(message, str): continue
            said, _, mark = message.rpartition("\n")
            if mark.strip() != self.mark(lane): continue
            match = REFUSAL.fullmatch(said.strip())
            if match is None: continue
            process, operation, target = match.groups()
            found.setdefault((operation, target or ""), {"operation": operation, "target": target or "",
                                                         "process": process})
        # What the lane's proxy refused, which the kernel never sees: a connection to a host the lane does not name.
        for target in self.proxies[lane].refused if lane in self.proxies else []:
            found.setdefault(("proxy-connect", target), {"operation": "proxy-connect", "target": target, "process": None})
        return list(found.values())[:KEPT]


def observed(sandboxed, refusals, lane, log_path, status):
    """Add to sandboxed, a receipt's record of lane's sandbox, what the kernel refused its commands (refused) and, for a
    command that failed as a program it started could not apply a sandbox of its own (NESTED), which program that was
    as the plugins tell (plugins.SANDBOX_NESTED) or that one was (nested). A failed command whose log says "Operation
    not permitted", of which no refusal was read, may have met one the kernel did not report (unreported)."""
    if not sandboxed or "inside" in sandboxed: return
    # Whether the kernel's log was read while it ran, proved to receive its refusals (Refusals.probed).
    sandboxed["refusalsRead"] = bool(refusals is not None and refusals.ready)
    refused = refusals.refused(lane) if refusals is not None else []
    if refused: sandboxed["refused"] = refused
    if status == 0: return
    try: text = Path(log_path).read_text(errors="replace")
    except OSError: return
    if NESTED.search(text):
        named = next((found for tell in plugins.SANDBOX_NESTED if (found := tell(text))), None)
        sandboxed["nested"] = named or "a program the command started"
    elif not refused and UNPERMITTED.search(text):
        sandboxed["unreported"] = True


def refusal_text(each):
    """`OPERATION TARGET (PROCESS)`, as `file-write-create /Users/x/.npmrc (node)`."""
    return " ".join(filter(None, (each["operation"], each["target"], each.get("process") and f"({each['process']})")))


def summary_lines(recorded):
    """The lines a run's summary says of what its sandbox refused (observed): recorded is a receipt's "sandbox", a
    lane's or a group's, {lane: its own}."""
    if not isinstance(recorded, dict): return []
    each = {None: recorded} if "profile" in recorded or "inside" in recorded else recorded
    lines = []
    for lane, said in each.items():
        if not isinstance(said, dict): continue
        tag = f" ({lane})" if lane else ""
        if said.get("nested"):
            lines.append(f"  sandbox{tag}: {said['nested']} applies a sandbox of its own, which macOS refuses inside "
                         "the lane's (sandbox_apply: Operation not permitted); run the lane without \"sandbox\"")
        if said.get("unreported"):
            lines.append(f"  sandbox{tag}: the log says Operation not permitted, and the kernel reported no refusal "
                         "of it: a write outside the checkout or a connection the lane does not name may have been "
                         "refused; the receipt lists what the sandbox allows")
        refused = said.get("refused") or []
        for item in refused[:SHOWN]:
            lines.append(f"  sandbox refused{tag}: {refusal_text(item)}")
        if len(refused) > SHOWN:
            lines.append(f"  ... and {len(refused) - SHOWN} more the sandbox refused, listed in the receipt")
    return lines


def check_here():
    """Why sandbox-exec cannot apply a trivial profile on this Mac, or None: `qk doctor`'s check, which another Mac
    makes as offload_host.sandbox_check."""
    from . import offload_host
    return offload_host.sandbox_check()["problem"]


def doctor_warnings(config, read_there=None, limit=None):
    """`qk doctor`'s warning for each Mac, this one and the farm's that take work now, where sandbox-exec is missing or
    applies no profile, when some lane has a sandbox: such a Mac ends a sandboxed lane not started. Another Mac is read
    with the others at once, within limit seconds; one that does not answer is unchecked, not warned of."""
    from . import farm, remote
    lanes = config.get("lanes") if isinstance(config.get("lanes"), dict) else {}
    if not any(isinstance(lane, dict) and lane.get("sandbox") for lane in lanes.values()): return []
    # A doctor run inside a sandbox cannot apply another, which says nothing of the Mac.
    if os.environ.get(INSIDE): return []
    limit = remote.SETTINGS_TIMEOUT if limit is None else limit
    read_there = read_there or (lambda host: remote.ask(host, {"sandbox": True}, timeout=limit, unsent=True))
    found = {"this Mac": {"problem": check_here()}}
    try: hosts = farm.destinations()
    except ValueError: hosts = ()
    found.update(remote.read_macs(hosts, read_there, limit))
    warnings = []
    for mac, reading in found.items():
        if not isinstance(reading, dict): continue
        if reading.get("problem"):
            warnings.append(f"{mac} cannot run a sandboxed lane, which ends there not started: {reading['problem']}")
    return warnings


class Proxy:
    """An HTTP proxy on localhost for a sandboxed lane that names hosts (hosts): it admits a CONNECT, or a plain request
    in absolute form, to a host and port the lane names, and answers any other 403, recording it (refused). The
    lane's profile lets its command reach the network only through it, so a host it does not name cannot be reached,
    by name or by address: the proxy resolves names itself, and an address is admitted only as the lane names it,
    which hosts never does. It runs in the process of `qk run`, outside the sandbox, on the Mac that runs the lane,
    in threads that end with the run (close), so that nothing outlives it. A client that ignores the proxy variables
    reaches nothing, which the kernel's log reports as any refused connection (vocaro/queuekeeper#266)."""

    def __init__(self, named):
        self.named = list(named)
        self.refused = []
        self.server = None
        self.port = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.open = set()

    def admits(self, host, port):
        """Whether host:port is one the lane names: the same name, or a name under a *.DOMAIN it names."""
        host = host.lower().rstrip(".").strip("[]")
        for pattern, allowed in self.named:
            if port != allowed: continue
            if pattern.startswith("*.") and host.endswith(pattern[1:]) and len(host) > len(pattern) - 1: return True
            if host == pattern: return True
        return False

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.serve, name=f"qk-sandbox-proxy-{self.port}", daemon=True).start()
        return self

    def close(self):
        self.closed.set()
        with self.lock:
            sockets = [self.server, *self.open]
            self.open.clear()
        for each in sockets:
            if each is None: continue
            try: each.close()
            except OSError: pass

    def serve(self):
        while not self.closed.is_set():
            try: client, _ = self.server.accept()
            except OSError: return
            with self.lock:
                if self.closed.is_set():
                    client.close()
                    return
                self.open.add(client)
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def refuse(self, client, target, status="403 Forbidden"):
        if target is not None:
            with self.lock:
                if target not in self.refused: self.refused.append(target)
        try: client.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        except OSError: pass

    def handle(self, client):
        upstream = None
        try:
            client.settimeout(PROXY_TIMEOUT)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) + len(chunk) > PROXY_HEAD: return self.refuse(client, None, "400 Bad Request")
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3: return self.refuse(client, None, "400 Bad Request")
            method, target, version = parts
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                if not port.isdigit() or not self.admits(host, int(port)): return self.refuse(client, target)
                upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=PROXY_TIMEOUT)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest: upstream.sendall(rest)
            else:
                match = re.fullmatch(r"http://([^/:\s]+|\[[^\]]+\])(?::([0-9]+))?(/[^\s]*)?", target, re.I)
                if match is None: return self.refuse(client, None, "400 Bad Request")
                host, port, path = match.group(1), int(match.group(2) or 80), match.group(3) or "/"
                if not self.admits(host, port): return self.refuse(client, f"{host}:{port}")
                upstream = socket.create_connection((host.strip("[]"), port), timeout=PROXY_TIMEOUT)
                kept = [line for line in lines[1:] if not line.lower().startswith(("proxy-connection:", "proxy-authorization:"))]
                upstream.sendall("\r\n".join([f"{method} {path} {version}", *kept]).encode("latin-1") + b"\r\n\r\n" + rest)
            with self.lock:
                if self.closed.is_set(): return
                self.open.add(upstream)
            self.relay(client, upstream)
        except OSError:
            return
        finally:
            for each in (client, upstream):
                if each is None: continue
                with self.lock: self.open.discard(each)
                try: each.close()
                except OSError: pass

    def relay(self, client, upstream):
        """Bytes each way until either side ends: the peer is fixed, so a later request on the same connection
        reaches only the host admitted."""
        client.settimeout(None)
        upstream.settimeout(None)
        pair = {client: upstream, upstream: client}
        while not self.closed.is_set():
            ready, _, _ = select.select(list(pair), [], [], 1)
            for source in ready:
                data = source.recv(65536)
                if not data: return
                pair[source].sendall(data)
