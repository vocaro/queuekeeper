"""A build that fails on a module cache older than a dependency's header builds once more without it.

A build folder reused across a dependency change can keep an explicit module built from the old header:
the build fails with "has been modified since the module file", then with errors for whatever the new
header added (vocaro-engineering#85, #267). queuekeeper run puts SHIMS first on every lane's PATH, so that a
lane's own `xcodebuild` or `swift`, however its script calls it by name and wherever it sends the
output, runs through main: a failed run whose output has that message removes the module-cache folder
the message names, inside the checkout the lane runs in, and runs once more. It never runs a third time.

Standard library only: it starts with every build a lane runs.
"""

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from ..shims import ROOT_VARIABLE, SHIMS, environment  # noqa: F401 -- environment is read here too

STALE_MODULE = "has been modified since the module file"
# The module file clang names; its folder is a cache, rebuilt from sources when missing.
MODULE_FILE = re.compile(r"has been modified since the module file '([^']+)' was built")
CACHES = ("SwiftExplicitPrecompiledModules", "ModuleCache.noindex", "ModuleCache")
TOOLS = ("xcodebuild", "swift")
RESULTS_VARIABLE = "QUEUEKEEPER_RESULTS_DIR"
RECORD = "stale-modules.log"
# What a wrapped run says when it rebuilds, which queuekeeper simulator-tests reads as handled already.
NOTICE = "queuekeeper: STALE MODULES"
MARKER = b"queuekeeper.apple import stale_modules"


def shim(path):
    try:
        with open(path, "rb") as file: return MARKER in file.read(2048)
    except OSError:
        return False


def real(tool, path=None):
    """The first tool on PATH that is not one of these wrappers, a copy in another toolkit's SHIMS too."""
    for entry in (os.environ.get("PATH", os.defpath) if path is None else path).split(os.pathsep):
        candidate = Path(entry or ".") / tool
        if candidate.is_file() and os.access(candidate, os.X_OK) and not shim(candidate): return str(candidate)
    return None


def folders(text, root, cwd="."):
    """The module-cache folders the stale-module errors in text name, each inside root and not root itself."""
    root = Path(root).resolve()
    found = []
    for named in dict.fromkeys(MODULE_FILE.findall(text)):
        module = (Path(cwd) / named).resolve()
        cache = next((parent for parent in module.parents if parent.name in CACHES), None)
        if cache is None or cache == root or root not in cache.parents: continue
        if cache not in found: found.append(cache)
    return found


def attempt(command):
    """Run command with its output passed through as it comes, and return its status, 128 + N for signal N, and
    the lines of its output that have STALE_MODULE."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # The tool shares this process group, so a stop sent to the group reaches it once, by itself. SIGTERM and
    # SIGHUP sent to this process alone are passed on, a second one being harmless; SIGINT is not, since a build
    # tool may take a second interrupt as a hurried cancel, and this waits for the tool to end in either case.
    forwarded = {number: signal.signal(number, lambda received, _frame: process.send_signal(received))
                 for number in (signal.SIGTERM, signal.SIGHUP)}
    forwarded[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
    errors, lock = [], threading.Lock()

    def copy(stream, descriptor):
        tail, open_ = b"", True
        for chunk in iter(lambda: os.read(stream.fileno(), 65536), b""):
            if open_:
                try:
                    view = memoryview(chunk)
                    while view: view = view[os.write(descriptor, view):]
                except BrokenPipeError:
                    open_ = False
            lines = (tail + chunk).split(b"\n")
            tail = lines.pop()
            matched = [line.decode(errors="replace") for line in lines if STALE_MODULE.encode() in line]
            if matched:
                with lock: errors.extend(matched)
        if STALE_MODULE.encode() in tail:
            with lock: errors.append(tail.decode(errors="replace"))

    readers = [threading.Thread(target=copy, args=(process.stdout, 1)),
               threading.Thread(target=copy, args=(process.stderr, 2))]
    for reader in readers: reader.start()
    try:
        status = process.wait()
        for reader in readers: reader.join()
    finally:
        for number, handler in forwarded.items(): signal.signal(number, handler)
    return (128 - status if status < 0 else status), "\n".join(errors)


def say(line):
    try:
        os.write(2, (line + "\n").encode())
    except OSError:
        pass


def record(tool, removed, status):
    results = os.environ.get(RESULTS_VARIABLE)
    if not results: return
    try:
        Path(results).mkdir(parents=True, exist_ok=True)
        with (Path(results) / RECORD).open("a") as file:
            file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {tool}: removed "
                       f"{', '.join(map(str, removed))}; the rebuild ended with status {status}\n")
    except OSError:
        pass


def main(tool, arguments):
    found = real(tool)
    if found is None:
        say(f"queuekeeper: no {tool} on PATH besides queuekeeper's wrapper in {SHIMS}")
        return 127
    command = [found, *arguments]
    status, errors = attempt(command)
    root = os.environ.get(ROOT_VARIABLE)
    if status == 0 or not errors or not root: return status
    removed = folders(errors, root, os.getcwd())
    if not removed:
        say(f"{NOTICE}: {tool} failed with \"{STALE_MODULE}\", and the module file it names is in no module cache "
            f"inside {root}, so nothing was removed and it is not run again")
        return status
    for folder in removed: shutil.rmtree(folder, ignore_errors=True)
    say(f"{NOTICE}: {tool} failed with \"{STALE_MODULE}\"; removed {', '.join(map(str, removed))}; running it once "
        "more (vocaro-engineering#267)")
    status, again = attempt(command)
    say(f"{NOTICE}: the rerun of {tool} ended with status {status}" +
        (", the same error again, so it is not run a third time" if status != 0 and again else ""))
    record(tool, removed, status)
    return status
