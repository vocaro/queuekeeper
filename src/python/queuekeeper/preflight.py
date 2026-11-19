"""`qk preflight`: the project's preflight lanes, run here, now, as the merge queue runs them before it queues an entry
(vocaro/queuekeeper#69).

A preflight lane is a cheap check the project defines, such as a spelling check, a linter or a formatter in check
mode: one command, which exits 0 when it passes, 1 when it finds something, and anything else when it could not run.
It prints each finding as `PATH:LINE: message`. This runs each in the working tree, uncommitted changes included,
without admission, in the order queue.preflight lists them, and keeps going after a failure so that one run shows
every finding. Exit 0 when every lane passed, 1 when one found something, 3 when none did but one could not run, and
2 for a lane name the project's preflight does not list."""
import os
import shlex
import sys
import time
from pathlib import Path

from . import processes
from .config import PREFLIGHT_LIMIT, PROJECT_CHECKOUT_VARIABLE, SPELLING, implicit_command, implicit_spelling, \
    legacy_spelling_warning, preflight, project_checkout

# The last lines of a failed lane's output that the summary repeats.
SHOWN = 40
UNMEASURED = 3


def command_of(root, config, name):
    """The command and limit of the preflight lane name, or (None, why) when it is no one command."""
    lane = config.get("lanes", {}).get(name)
    if lane is None and name == SPELLING and implicit_spelling(config):
        return implicit_command(Path(root), SPELLING), PREFLIGHT_LIMIT
    command = lane.get("command") if isinstance(lane, dict) else None
    if not (isinstance(command, list) and command): return None, f"{name} is no lane with one command"
    return command, min(lane.get("timeout", PREFLIGHT_LIMIT), PREFLIGHT_LIMIT)


def run(root, config, names=(), out=print):
    """Run the preflight lanes names, or all of them, in the checkout at root; return the exit status."""
    listed = preflight(config)
    unknown = [name for name in names if name not in listed]
    if unknown:
        out(f"qk preflight: {', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not among the project's "
            f"preflight lanes ({', '.join(listed) or 'none'})")
        return 2
    warned = legacy_spelling_warning(config)
    if warned: print(f"qk preflight: {warned}", file=sys.stderr)
    chosen = list(names) or listed
    if not chosen:
        out("qk preflight: the project has no preflight lanes (queue.preflight)")
        return 0
    directory = Path(root) / ".build/queuekeeper/preflight"
    directory.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, PROJECT_CHECKOUT_VARIABLE: project_checkout(root)}
    failed, unmeasured = [], []
    for name in chosen:
        command, limit = command_of(root, config, name)
        if command is None:
            out(f"{name}: could not run: {limit}")
            unmeasured.append(name)
            continue
        log = directory / f"{name}.log"
        began = time.monotonic()
        try: status = processes.run(command, cwd=root, log_path=log, timeout=limit, env=env)
        except processes.TimedOut as error:
            out(f"{name}: could not run: {shlex.join(command)} reached its limit of {limit}s ({error})")
            unmeasured.append(name)
            continue
        except OSError as error:
            out(f"{name}: could not run: {shlex.join(command)}: {error}")
            unmeasured.append(name)
            continue
        took = time.monotonic() - began
        if status == 0:
            out(f"{name}: passed in {took:.1f}s")
            continue
        printed = [line for line in log.read_text(errors="replace").splitlines() if line.strip()]
        if status == 1:
            out(f"{name}: FAILED in {took:.1f}s: {shlex.join(command)} found something:")
            failed.append(name)
        else:
            out(f"{name}: could not run: {shlex.join(command)} exited {status}, which is no finding:")
            unmeasured.append(name)
        for line in printed[-SHOWN:]: out(f"  {line}")
        if len(printed) > SHOWN: out(f"  (the whole output is in {log})")
    passed = len(chosen) - len(failed) - len(unmeasured)
    out(f"qk preflight: {passed} passed, {len(failed)} failed, {len(unmeasured)} could not run")
    return 1 if failed else UNMEASURED if unmeasured else 0
