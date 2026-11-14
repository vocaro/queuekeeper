"""Persistent public admission names. Lock nonces remain internal; older dashboard links still resolve.

Allocate names under one file lock so concurrent dashboard requests and servers cannot reuse a public name.
A request keeps its first name when its observations or metadata change, and when it starts running.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex

from ...merge_queue import entry_slug
from . import registry


def path():
    return registry.path().with_name("dashboard-admission-ids.json")


def slug(found):
    origin = found.get("origin") or {}
    branch, repository = origin.get("branch"), origin.get("repository")
    label = str(found.get("label") or "work")
    if branch and re.fullmatch(r"worktree-(?:agent|codex)-[a-z0-9]+", branch): branch = None
    try: words = shlex.split(label)
    except ValueError: words = label.split()
    description = label
    if words and ("/" in words[0] or any(word.startswith("-") for word in words)):
        command = Path(words[0]).name
        arguments = words[1:]
        if command in ("python", "python3") and arguments and not arguments[0].startswith("-"):
            command, arguments = Path(arguments[0]).name, arguments[1:]
        description = " ".join([command, *arguments[:1]]) if arguments and not arguments[0].startswith("-") else command
    if repository and not description.lower().startswith(repository.lower()): description = repository + " " + description
    return entry_slug(branch, description, label)


def names_for(reading):
    requests = []
    active = set()
    for host in (reading or {}).get("hosts", []):
        for found in [*(host.get("holders") or []), *(host.get("waiters") or [])]:
            identities = list(dict.fromkeys(value for value in (found.get("request"), found.get("identity"))
                                            if isinstance(value, str) and value))
            if identities:
                requests.append((identities, found))
                active.update(identities)
    if not requests: return {}
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try: stored = json.loads(target.read_text())
        except FileNotFoundError: stored = {}
        if not isinstance(stored, dict): raise ValueError("the dashboard admission names are not an object")
        names = {key: value for key, value in stored.items() if isinstance(key, str) and isinstance(value, str)}
        used, changed = set(names.values()), False
        for identities, found in requests:
            existing = next((names[identity] for identity in identities if identity in names), None)
            if existing:
                for identity in identities:
                    if identity not in names:
                        names[identity], changed = existing, True
                continue
            description = slug(found)
            candidate = None
            for digits in (4, 6, 8):
                for _ in range(100):
                    proposed = f"qk{secrets.randbelow(10 ** digits):0{digits}d}-{description}"
                    if proposed not in used:
                        candidate = proposed
                        break
                if candidate: break
            if candidate is None: raise RuntimeError("could not allocate an admission name")
            for identity in identities: names[identity] = candidate
            used.add(candidate)
            changed = True
        if changed:
            staged = target.with_suffix(f".{os.getpid()}.tmp")
            staged.write_text(json.dumps(names, indent=2) + "\n")
            os.replace(staged, target)
        return {identity: names[identity] for identity in active}
