"""The dashboard's server: `qk serve`.

    /                                  the page, which is also every path below /r/
    /static/NAME                       the page's own script and styles
    /panels/NAME.js                    a panel's script
    /api/panels                        the panels, in tab order
    /api/repositories                  the repositories, each with what its queue is doing
    /api/r/REPOSITORY/panel/NAME       a panel's data for a repository, the request's query passed to the panel

It answers GET alone, and only a request whose Host names this Mac: a page elsewhere that pointed a name of its own
at this address would otherwise read the queue through its visitor's browser. It binds 127.0.0.1 unless told
otherwise, and says so when it is told."""
import argparse
import http.server
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qsl, unquote, urlsplit
import webbrowser

from ... import git_state
from . import HELP, panels, registry, repository

PORT, BIND = 8750, "127.0.0.1"
STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}
LOCAL = {"localhost", "127.0.0.1", "[::1]"}
POLICY = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class Refused(Exception):
    def __init__(self, status, words):
        super().__init__(words)
        self.status = status


class App:
    """What the server serves: the repositories given, and those the registry lists, read again at each listing."""

    def __init__(self, given=()):
        self.given = [Path(root).resolve() for root in given]
        self.lock = threading.Lock()
        self.repositories = {}
        self.refresh()

    def refresh(self):
        roots = list(dict.fromkeys([*self.given, *registry.known()]))
        with self.lock:
            by_root = {found.root: found for found in self.repositories.values()}
            fresh = repository.named(roots)
            # One already served keeps what it has read.
            self.repositories = {name: by_root.get(found.root, found) for name, found in fresh.items()}
            for name, found in self.repositories.items(): found.name = name
            return dict(self.repositories)

    def repository(self, name):
        with self.lock: found = self.repositories.get(name)
        if found is None: found = self.refresh().get(name)
        if found is None: raise Refused(404, f"no repository is named {name}")
        return found

    def listing(self):
        home = panels.load("home")
        found = []
        for name, one in self.refresh().items():
            try:
                level, said, detail = home.headline(one.snapshot(), None)
                found.append({"name": name, "level": level, "headline": said, "detail": detail,
                              "waiting": len(one.snapshot().get("waiting") or [])})
            except Exception as error:  # noqa: BLE001
                found.append({"name": name, "level": "warning", "headline": "Cannot be read", "detail": str(error),
                              "waiting": 0})
        return found

    def answer(self, path, query):
        """The JSON a path below /api/ is answered with."""
        parts = [unquote(part) for part in path.split("/") if part]
        if parts == ["panels"]: return panels.listed()
        if parts == ["repositories"]: return self.listing()
        if len(parts) == 4 and parts[0] == "r" and parts[2] == "panel":
            found = self.repository(parts[1])
            try: module = panels.load(parts[3])
            except LookupError as error: raise Refused(404, str(error)) from None
            except Exception as error:  # noqa: BLE001
                raise Refused(500, f"the {parts[3]} panel did not load: {error}") from None
            try: return module.data(found, query)
            except LookupError as error: raise Refused(404, str(error)) from None
            except Exception as error:  # noqa: BLE001
                raise Refused(500, f"the {parts[3]} panel has a fault: {error}") from None
        raise Refused(404, "no such address")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "queuekeeper"
    protocol_version = "HTTP/1.1"

    def log_message(self, *arguments):  # the server's own log would be one line a poll
        pass

    def send(self, status, body, kind):
        self.send_response(status)
        for name, value in (("Content-Type", kind), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                            ("Content-Security-Policy", POLICY), ("X-Content-Type-Options", "nosniff"),
                            ("Referrer-Policy", "no-referrer")):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def json(self, status, found):
        self.send(status, json.dumps(found).encode(), "application/json; charset=utf-8")

    def file(self, path):
        try: body = path.read_bytes()
        except OSError: raise Refused(404, "no such file") from None
        self.send(200, body, TYPES.get(path.suffix, "application/octet-stream"))

    def named_here(self):
        host = self.headers.get("Host") or ""
        # Without its port: an IPv6 address is in brackets, which hold colons of their own.
        host = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
        return host in LOCAL or host.strip("[]") == self.server.bound

    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if not self.named_here(): raise Refused(403, "this server answers only a request for this Mac by name")
            path = url.path
            if path.startswith("/api/"):
                return self.json(200, self.server.app.answer(path[len("/api"):], dict(parse_qsl(url.query))))
            if path == "/" or path == "/r" or path.startswith("/r/"): return self.file(STATIC / "index.html")
            name = path.rsplit("/", 1)[1]
            if path == f"/static/{name}" and name in {found.name for found in STATIC.iterdir() if found.is_file()}:
                return self.file(STATIC / name)
            if path == f"/panels/{name}" and name.endswith(".js") and name[:-3] in panels.available():
                return self.file(panels.FOLDER / name)
            raise Refused(404, "no such address")
        except Refused as refused:
            self.json(refused.status, {"error": str(refused)})
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as error:  # noqa: BLE001
            with_suppressed(lambda: self.json(500, {"error": f"the server has a fault: {error}"}))

    do_HEAD = do_GET

    def refuse(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse


def with_suppressed(call):
    try: call()
    except Exception:  # noqa: BLE001
        pass


def make(given=(), port=PORT, bind=BIND):
    """A server of given repositories and the registry's, bound and not yet serving."""
    server = http.server.ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.app, server.bound = App(given), bind
    return server


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk serve", description=HELP)
    parser.add_argument("repositories", nargs="*", type=Path, metavar="REPOSITORY", help="a repository to show, by a "
                        "path to a checkout of it, beside this project's and those whose queues have run while the "
                        "feature was on")
    parser.add_argument("--port", type=int, default=PORT, help=f"the port to serve on (default {PORT}; 0 for any free one)")
    parser.add_argument("--bind", default=BIND, metavar="ADDRESS", help=f"the address to serve on (default {BIND}, this "
                        "Mac alone); any other lets every machine that can reach it read the queue and its logs")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args(argv)
    roots = []
    for folder in [Path(project or "."), *args.repositories]:
        folder = folder.resolve()
        root = git_state.main_checkout(folder) or folder
        if (root / ".git").is_dir(): roots.append(root)
        elif folder != Path(project or ".").resolve():
            print(f"queuekeeper: {folder} is no checkout of a repository", file=sys.stderr)
            return 2
    for root in roots: registry.add(root)
    try: server = make(roots, args.port, args.bind)
    except OSError as error:
        print(f"queuekeeper: cannot serve on {args.bind}:{args.port}: {error}", file=sys.stderr)
        return 2
    address = f"http://{'localhost' if args.bind == BIND else args.bind}:{server.server_address[1]}/"
    print(f"queuekeeper: serving {len(server.app.repositories)} repositor{'y' if len(server.app.repositories) == 1 else 'ies'} "
          f"at {address}; Ctrl-C stops it", flush=True)
    if args.bind not in (BIND, "localhost", "::1"):
        print(f"queuekeeper: {args.bind} is not this Mac alone: every machine that can reach it can read the queue "
              "and its logs", file=sys.stderr, flush=True)
    if args.open: webbrowser.open(address)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0
