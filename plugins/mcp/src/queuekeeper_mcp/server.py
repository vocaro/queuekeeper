"""An MCP server over stdio that lets an agent land and follow work through Queuekeeper's JSON commands.

It speaks JSON-RPC 2.0, one message a line, with the standard library alone. Each tool runs the project's own
launcher, `scripts/qk`, with `--json`, and returns the one JSON result that command prints on standard output.
The commands stay the authority for every action, state and exit status; this server only carries them. Lines a
command writes to standard error while it runs, such as `queue wait`'s changes of situation and heartbeats, go to
the client as progress notifications when its request asked for progress.

Run it as `python3 PATH/TO/server.py`, optionally with `--project DIR` (the default directory for tools, which is
otherwise the server's working directory) and `--launcher PATH` (a `qk` launcher to use instead of the nearest
`scripts/qk` above the directory a tool runs in).
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

NAME = "queuekeeper"
VERSION = "1.0.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
WAIT_LIMIT = 600
KEPT_ERROR_LINES = 40
READER_GRACE = 60

DIRECTORY = {"type": "string", "description": "the checkout or worktree to run in: the commit's worktree for `land` "
                                               "(default: the server's project directory)"}
ENTRY = {"type": "string", "description": "a queue entry ID, as `land` returns it"}

TOOLS = [
    {"name": "land",
     "description": "Submit a finished commit to this project's merge queue and return at once with the new entry "
                    "(`qk land REF --json`). Follow it with queue_wait, and read its verdict with queue_result. An exit "
                    "status of 1 means the submission was refused; the result's `recovery` says what to do next.",
     "inputSchema": {"type": "object", "properties": {
         "reference": {"type": "string", "description": "the commit or branch to land (default: HEAD of directory)"},
         "note": {"type": "string", "description": "what the entry is, for `qk queue status`"},
         "directory": DIRECTORY}, "additionalProperties": False}},
    {"name": "queue_wait",
     "description": "Follow one or more queue entries to their results (`qk queue wait ID... --json`), reporting each "
                    "change of situation as a progress notification. Exit status 0 means landed, 1 refused or sent "
                    "back, and 75 held or the wait reached its limit; ending the wait never withdraws an entry, so "
                    "call it again to keep following.",
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": ENTRY, "minItems": 1},
         "limit": {"type": "number", "exclusiveMinimum": 0,
                   "description": f"seconds to wait before returning with exit status 75 (default {WAIT_LIMIT})"},
         "directory": DIRECTORY}, "required": ["ids"], "additionalProperties": False}},
    {"name": "queue_result",
     "description": "Read one entry's durable result, including settled entries, without starting a worker "
                    "(`qk queue result ID --json`). Exit status 0 means the lookup succeeded, not that the gate "
                    "passed: read `state` and `terminal`.",
     "inputSchema": {"type": "object", "properties": {"id": ENTRY, "directory": DIRECTORY},
                     "required": ["id"], "additionalProperties": False}},
    {"name": "queue_status",
     "description": "Say what the queue is waiting for, running and holding (`qk queue status --json`).",
     "inputSchema": {"type": "object", "properties": {"directory": DIRECTORY}, "additionalProperties": False}},
    {"name": "queue_logs",
     "description": "Read a bounded piece of an entry's exact log (`qk queue logs ID --json`). Start without a "
                    "cursor, keep each chunk's `content`, and pass `nextCursor` back unchanged while `hasMore` is "
                    "true. A stale cursor exits 1 with `log-cursor-stale`: start over.",
     "inputSchema": {"type": "object", "properties": {
         "id": ENTRY,
         "cursor": {"type": "string", "description": "nextCursor from the previous read"},
         "limit": {"type": "integer", "minimum": 4, "maximum": 1048576,
                   "description": "maximum bytes per read (default 16384)"},
         "offset": {"type": "integer", "minimum": 0, "description": "starting byte offset, for a read without a cursor"},
         "artifact": {"type": "string", "description": "a recorded log kind or full path, from the entry's artifacts"},
         "directory": DIRECTORY}, "required": ["id"], "additionalProperties": False}},
]
NAMED = {tool["name"]: tool for tool in TOOLS}


class Invalid(ValueError):
    """Arguments the tool's schema does not allow."""


def command(name, arguments):
    """The qk arguments for one tool call."""
    tool = NAMED.get(name)
    if tool is None: raise Invalid(f"unknown tool {name!r}")
    if not isinstance(arguments, dict): raise Invalid("arguments must be an object")
    schema = tool["inputSchema"]
    unknown = set(arguments) - set(schema["properties"])
    if unknown: raise Invalid(f"{name} takes no {', '.join(sorted(unknown))}")
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing: raise Invalid(f"{name} needs {', '.join(missing)}")
    for key, value in arguments.items():
        kind = schema["properties"][key].get("type")
        valid = {"string": lambda v: isinstance(v, str) and v != "",
                 "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                 "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0,
                 "array": lambda v: isinstance(v, list) and v and all(isinstance(i, str) and i for i in v)}[kind]
        if not valid(value): raise Invalid(f"{name}'s {key} must be a non-empty {kind}")
    # Options go as --key=VALUE, which takes any value, but a positional that starts with '-' would be an option.
    for key in ("reference", "id", "ids"):
        values = arguments.get(key, [])
        if any(value.startswith("-") for value in ([values] if isinstance(values, str) else values)):
            raise Invalid(f"{name}'s {key} cannot start with '-'")
    if name == "land":
        reference = arguments.get("reference")
        return ["land", *([reference] if reference else []), "--json",
                *([f"--note={arguments['note']}"] if "note" in arguments else [])]
    if name == "queue_wait":
        return ["queue", "wait", *arguments["ids"], f"--limit={arguments.get('limit', WAIT_LIMIT)}", "--json"]
    if name == "queue_result":
        return ["queue", "result", arguments["id"], "--json"]
    if name == "queue_status":
        return ["queue", "status", "--json"]
    options = [f"--{key}={arguments[key]}" for key in ("cursor", "limit", "offset", "artifact") if key in arguments]
    return ["queue", "logs", arguments["id"], "--json", *options]


def last_object(text):
    """The last JSON object in a command's standard output, which ends with its one result, or None. A line printed
    ahead of it, such as one a hook or a recovery prints before the command routes its output, does not hide it."""
    decoder = json.JSONDecoder()
    for start in reversed([0, *(i + 1 for i, c in enumerate(text) if c == "\n")]):
        if not text.startswith("{", start): continue
        try:
            found, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(found, dict) and not text[end:].strip(): return found
    return None


def ended(status):
    """How a command ended: its exit status, or the signal that ended it, such as SIGTERM from a server stopping."""
    if status >= 0: return f"exited {status}"
    try:
        name = signal.Signals(-status).name
    except ValueError:
        name = f"signal {-status}"
    return f"was ended by {name} (exited {status})"


def launcher_for(directory, configured=None):
    """The configured launcher, else the nearest `scripts/qk` at or above directory."""
    if configured: return Path(configured)
    for folder in [directory, *directory.parents]:
        candidate = folder / "scripts" / "qk"
        if candidate.is_file(): return candidate
    raise Invalid(f"no scripts/qk at or above {directory}; pass the server --launcher, or give a directory "
                  "inside a project that has Queuekeeper's launcher")


class Server:
    def __init__(self, output, *, project=None, launcher=None, python=sys.executable):
        self.output = output
        self.project = Path(project or os.getcwd()).resolve()
        self.launcher = launcher
        self.python = python
        self.writing = threading.Lock()
        self.running = {}
        self.cancelled = set()
        self.guard = threading.Lock()
        self.threads = []
        self.stopping = False

    def send(self, message):
        line = json.dumps(message, separators=(",", ":"))
        with self.writing:
            self.output.write(line + "\n")
            self.output.flush()

    def reply(self, identifier, result=None, error=None):
        with self.guard:
            if identifier in self.cancelled:
                self.cancelled.discard(identifier)
                return
        message = {"jsonrpc": "2.0", "id": identifier}
        if error is not None: message["error"] = error
        else: message["result"] = result
        self.send(message)

    def handle_line(self, line):
        line = line.strip()
        if not line: return
        try:
            message = json.loads(line)
        except ValueError as error:
            self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {error}"}})
            return
        for each in message if isinstance(message, list) else [message]:
            self.handle(each)

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            return
        method, identifier, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method is None: return  # a response to a request this server never sends
        if "id" not in message:
            if method == "notifications/cancelled": self.cancel(params.get("requestId"))
            return
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.reply(identifier, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": VERSION},
                "instructions": "Land commits through Queuekeeper's merge queue and follow them to their results. "
                                "Submit with land, then follow the returned id with queue_wait until its result is "
                                "terminal; on exit status 75, call queue_wait again. Read failures with "
                                "queue_result and full logs with queue_logs, passing nextCursor back unchanged."})
        elif method == "ping":
            self.reply(identifier, {})
        elif method == "tools/list":
            self.reply(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            thread = threading.Thread(target=self.call, args=(identifier, params), daemon=True)
            self.threads.append(thread)
            thread.start()
        else:
            self.reply(identifier, error={"code": -32601, "message": f"method not found: {method}"})

    def cancel(self, identifier):
        with self.guard:
            process = self.running.get(identifier)
            if process is None: return
            self.cancelled.add(identifier)
        process.terminate()

    def call(self, identifier, params):
        name, arguments = params.get("name"), params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        try:
            argv = command(name, arguments)
            directory = Path(arguments.get("directory") or self.project).expanduser()
            if not directory.is_absolute(): directory = self.project / directory
            if not directory.is_dir(): raise Invalid(f"no directory {directory}")
            launcher = launcher_for(directory.resolve(), self.launcher)
        except Invalid as error:
            if name not in NAMED:
                self.reply(identifier, error={"code": -32602, "message": str(error)})
            else:
                self.reply(identifier, {"content": [{"type": "text", "text": str(error)}], "isError": True})
            return
        self.reply(identifier, self.run(identifier, [self.python, str(launcher), *argv], directory, token))

    def run(self, identifier, argv, directory, token):
        try:
            process = subprocess.Popen(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, errors="replace")
        except OSError as error:
            return {"content": [{"type": "text", "text": f"could not run {argv[1]}: {error}"}], "isError": True}
        with self.guard:
            self.running[identifier] = process
            if self.stopping: process.terminate()
        out, err = [], []

        def progress():
            for count, line in enumerate(process.stderr, 1):
                line = line.rstrip("\n")
                err.append(line)
                del err[:-KEPT_ERROR_LINES]
                if token is not None and line.strip():
                    self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                               "params": {"progressToken": token, "progress": count, "message": line}})

        readers = [threading.Thread(target=lambda: out.append(process.stdout.read()), daemon=True),
                   threading.Thread(target=progress, daemon=True)]
        for reader in readers: reader.start()
        status = process.wait()
        # Both end with the command's output unless something it started holds them open; on a loaded Mac a reader
        # can take a while to be scheduled, so the bound is generous.
        for reader in readers: reader.join(timeout=READER_GRACE)
        with self.guard: self.running.pop(identifier, None)
        text = "".join(out)
        payload = last_object(text)
        summary = f"qk {' '.join(argv[2:])} {ended(status)}"
        if not isinstance(payload, dict):
            # Everything the command said, so a client or a test that finds no JSON can say why.
            detail = "\n".join([summary, "standard output:" if text.strip() else "no standard output",
                                 *([text.strip()] if text.strip() else []),
                                 *(["standard error:", *err] if any(line.strip() for line in err) else [])])
            return {"content": [{"type": "text", "text": detail}], "isError": True}
        payload.setdefault("exitStatus", status)
        return {"content": [{"type": "text", "text": f"{summary}\n{json.dumps(payload, indent=2)}"}],
                "structuredContent": payload, "isError": status in {2, 70}}

    def serve(self, lines):
        for line in lines:
            self.handle_line(line)
        # Closing standard input is how a client starts to shut the server down, and it may still read the replies
        # to calls it already sent, so they finish; a client that will not wait stops the server with SIGTERM.
        for thread in self.threads: thread.join()

    def stop(self):
        """End every command running and any that starts from now on. Ending a wait withdraws nothing."""
        with self.guard:
            self.stopping = True
            processes = list(self.running.values())
        for process in processes: process.terminate()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="queuekeeper-mcp", description=__doc__.split("\n")[0])
    parser.add_argument("--project", help="the default directory for tools (default: the working directory)")
    parser.add_argument("--launcher", help="the qk launcher to run (default: the nearest scripts/qk)")
    args = parser.parse_args(argv)
    stdin = open(sys.stdin.fileno(), encoding="utf-8", errors="replace", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)
    serving = Server(stdout, project=args.project, launcher=args.launcher)

    def stopped(number, frame):
        serving.stop()
        os._exit(128 + number)
    # Claude Code stops its servers with SIGINT and then SIGTERM; either ends the commands they run.
    for number in (signal.SIGINT, signal.SIGTERM): signal.signal(number, stopped)
    serving.serve(stdin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
