"""A run's pin set: one commit for each folder beside the project that its extensions load from.

A project's queuekeeper.json may load an extension from a folder beside it, `../NAME/...`, and that folder's own
queuekeeper.json may name, under "siblings", further folders its code loads from, each with the environment variable
it finds the folder by (config.sibling_variables).
Queuekeeper itself is held still for a run by QUEUEKEEPER_ROOT; those folders were not, so a landing in one while a
run of hours went on reached the run's later lanes and the qk commands nested in them, which then loaded an adapter
that no longer matched the Queuekeeper they ran (vocaro-engineering#497).

What runs lanes pins them: the merge queue's worker for each round, `qk run`, and a caller that runs lanes one after
another, such as the toolkit's `eng checkpoint`. Each folder is pinned at its origin/main, as this Mac's own checkout of
it names that, unless `qk run --sibling NAME=WHERE` chooses another commit or checkout. The set travels in
QUEUEKEEPER_SIBLINGS, {NAME: {"commit", "root"}} as JSON, and every qk command that finds it set loads the project's
extensions from each root in place of the folder (config.extensions_of), sets each folder's variable, where it has
one, to its root for itself and what it starts (exported), and makes no set of its own. A root is the Mac's one
checkout of that folder at that commit (offload_host.pinned_checkout), which nothing moves.

Nothing is sent to the second Mac but the commits: it takes each from its own checkout of the folder
(offload_host.place_siblings). A commit its origin lacks, one chosen with --sibling that is not pushed, therefore
runs on this Mac only.
"""
import json
import os
from pathlib import Path
import subprocess

from . import git_state, offload_host

VARIABLE = offload_host.PIN_VARIABLE
MAIN = "refs/remotes/origin/main"


class Unpinned(RuntimeError):
    """A pin set that cannot be made or used. Raised while Queuekeeper starts, it ends the command as one that could
    not start (cli.could_not_start): nothing ran, and it says nothing of the project's commits."""


def held(variables=None):
    """The pin set this process runs under, {NAME: {"commit", "root"}}, or None when it runs under none."""
    text = (os.environ if variables is None else variables).get(VARIABLE)
    if not text: return None
    try:
        pins = json.loads(text)
        if not isinstance(pins, dict) or not all(isinstance(pin, dict) and isinstance(pin.get("commit"), str)
                                                 and isinstance(pin.get("root"), str) for pin in pins.values()):
            raise ValueError("it names no commit and root for each folder")
    except ValueError as error:
        raise Unpinned(f"{VARIABLE} holds no pin set: {error}")
    return pins


def verify(pins):
    """Refuse a pin set whose checkouts are not what it says: a folder the Mac that made it could not take (its
    "error"), a root that is no longer there, as when the set outlived its run, or one at another commit. Reading a
    checkout's HEAD runs no Git, since every qk command under a pinned run does this."""
    for name, pin in (pins or {}).items():
        at = f"{name} pinned at {pin['commit'][:12]} for this run"
        if pin.get("error"): raise Unpinned(f"{at} could not be had on this Mac: {pin['error']}")
        found = offload_host.pin_head(pin["root"])
        if found is None: raise Unpinned(f"{at} has no checkout at {pin['root']}, which {VARIABLE} names")
        if found != pin["commit"]:
            raise Unpinned(f"{at} is at {found[:12]} in {pin['root']}, which {VARIABLE} names")


def folder(path, pins):
    """Where an extension's folder `../NAME/REST` is under pins: REST in NAME's pinned root, or None for a path
    that names no pinned folder, which is then found beside the project as ever."""
    parts = Path(path).parts
    if not pins or len(parts) < 2 or parts[0] != ".." or parts[1] not in pins: return None
    return str(Path(pins[parts[1]]["root"]).joinpath(*parts[2:]))


def clone(root, name):
    """This Mac's own checkout of name, a folder beside the project at root: beside its main checkout."""
    return (git_state.main_checkout(root) or Path(root)).resolve().parent / name


def git(directory, *arguments):
    try: return offload_host.pin_git(directory, *arguments)
    except (RuntimeError, OSError) as error: raise Unpinned(str(error))


def main_commit(checkout, fetch=False):
    """The commit checkout's origin/main names, fetched first with fetch."""
    if fetch: git(checkout, "fetch", "--no-tags", "--quiet", "origin", f"refs/heads/main:{MAIN}")
    return git(checkout, "rev-parse", "--verify", "--quiet", f"{MAIN}^{{commit}}")


def pushed(checkout, commit):
    """Whether a branch of checkout's origin holds commit, as its remote-tracking refs say, so that the second Mac
    can fetch it."""
    try: return bool(git(checkout, "for-each-ref", "--count=1", "--contains", commit, "refs/remotes"))
    except Unpinned: return False


def chosen(root, names, given):
    """What `--sibling NAME=WHERE`, each of given, chooses: {NAME: (checkout to take it from, commit, WHERE)}.
    WHERE is a commit of this Mac's checkout of NAME, or a checkout's path, whose working tree it then is: HEAD, or
    a snapshot commit of its changes, which no branch holds (remote.snapshot). A ValueError says what is wrong with
    one."""
    choices = {}
    for entry in given:
        name, _, where = entry.partition("=")
        if not name or not where: raise ValueError(f"--sibling {entry} names no folder and commit: --sibling NAME=WHERE")
        if name not in names:
            raise ValueError(f"--sibling names {name}, which is no folder the project's extensions load from"
                             + (f": they load from {', '.join(names)}" if names else ", as they load from none"))
        if name in choices: raise ValueError(f"--sibling names {name} twice")
        path = Path(where).expanduser()
        if path.is_dir():
            from . import remote  # which imports config, which imports this module
            try: commit = remote.snapshot(path.resolve())[0]
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                raise ValueError(f"--sibling {entry}: no commit holds {path}'s working tree: {error}")
            choices[name] = (path.resolve(), commit, where)
            continue
        own = clone(root, name)
        try: commit = git(own, "rev-parse", "--verify", "--quiet", f"{where}^{{commit}}")
        except Unpinned: raise ValueError(f"--sibling {entry}: {where} is no directory and no commit of {own}")
        choices[name] = (own, commit, where)
    return choices


def make(root, names, choices=None, fetch=False, say=None):
    """The pin set for a run of the project at root: each of names, the folders its configuration declares
    (config.siblings_in; a list names them with no variable), at its origin/main as this Mac's checkout of it holds
    that, fetched first with fetch, which may be the function that fetches a checkout, or as choices says (chosen),
    with this Mac's one checkout of it at that commit made if it is not there. A folder chosen by hand says so
    ("chosen"), one whose commit no branch of its origin holds says "local", as the second Mac cannot take it, and
    one that code finds by an environment variable names it ("variable").

    Each folder a pinned one declares in turn, in its queuekeeper.json at the commit pinned
    (config.further_siblings), is pinned by the same rules: the toolkit, loaded as a project's extension, loads
    from two more.

    A folder that is no Git checkout, or whose checkout names no origin/main, has no commit to pin: it is left out,
    and loads as it stands. So is a folder found by a variable that this Mac has nothing of. An extension's folder
    this Mac has nothing of, or a commit that cannot be checked out, is Unpinned."""
    from . import config  # which imports this module
    wanted = dict(names) if isinstance(names, dict) else dict.fromkeys(names)
    pins, waiting = {}, list(wanted)
    while waiting:
        name = waiting.pop(0)
        own, commit, where = (choices or {}).get(name) or (clone(root, name), None, None)
        if not own.is_dir() and wanted.get(name):
            # Found by a variable, by code that does without it where the Mac has none.
            if say: say(f"no {name}: this Mac has no {own}")
            continue
        if not own.is_dir():
            raise Unpinned(f"this Mac has no {own}, which the project's extension loads from: clone it there")
        if commit is None:
            further = config.further_siblings(own)
            if (own / ".git").exists():
                if callable(fetch): fetch(own)
                try: commit = main_commit(own, fetch is True)
                except Unpinned:
                    if say: say(f"{name} as {own} stands: it names no origin/main to pin it at")
            if commit is None:
                # No commit to pin it at: it loads as it stands, and so does what it declares, by that checkout.
                for more, variable in further.items():
                    if more not in wanted: waiting.append(more)
                    if not wanted.get(more): wanted[more] = variable
                continue
        try: checkout = offload_host.pinned_checkout(own, name, commit, fetch=False)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            raise Unpinned(f"{name} could not be pinned at {commit[:12]}: {error}")
        for more, variable in config.further_siblings(checkout).items():
            if more not in wanted: waiting.append(more)
            if not wanted.get(more): wanted[more] = variable
        pins[name] = {"commit": commit, "root": str(checkout), **({"chosen": where} if where else {}),
                      **({} if pushed(own, commit) else {"local": True})}
        if say: say(f"{name} at {commit[:12]}, " + (f"chosen with --sibling {name}={where}" if where else "its origin/main")
                    + f", from {checkout}")
    for name, pin in pins.items():
        if wanted.get(name): pin["variable"] = wanted[name]
    return pins


def exported(pins):
    """What a command under pins sets in its environment, for itself and all it starts: each folder's variable, the
    one its code finds the folder by, as the root the run pinned. So the toolkit, loaded from its pinned checkout,
    loads apple-engineering and AXVerify from theirs, and so does every eng command of the lane."""
    return {pin["variable"]: pin["root"] for pin in (pins or {}).values()
            if isinstance(pin.get("variable"), str) and not pin.get("error")}


def encoded(pins):
    return json.dumps(pins, sort_keys=True)


def local(pins):
    """The folders of pins that the second Mac cannot take, each a commit no branch of its origin holds."""
    return sorted(name for name, pin in (pins or {}).items() if pin.get("local"))


def dispatched(root, names):
    """What a dispatch asks the second Mac to place for the project at root, for each folder of the run's pin set and
    each of names, {NAME: VARIABLE or None} (config.sibling_variables), that the set lacks: {"name", "commit"}, the
    commit the set holds, and for work that runs under none, such as `qk admit`, the folder's origin/main as this
    Mac's checkout of it names that, which is where the second Mac's own checkout of it stood for such work before;
    and "variable", where the folder has one. A folder with no commit to name, as one that is no Git checkout here,
    has none, and the second Mac loads its own as it stands."""
    pins, wanted = held() or {}, []
    names = dict(names) if isinstance(names, dict) else dict.fromkeys(names)
    for name in [*pins, *(name for name in names if name not in pins)]:
        try: commit = pins[name]["commit"] if name in pins else main_commit(clone(root, name))
        except Unpinned: commit = None
        variable = pins[name].get("variable") if name in pins else names.get(name)
        wanted.append({"name": name, **({"commit": commit} if commit else {}), **({"variable": variable} if variable else {})})
    return wanted


def recorded(pins):
    """A pin set as a receipt holds it: each folder's commit, its root, and how it was chosen when by hand."""
    return {name: {key: pin[key] for key in ("commit", "root", "chosen", "local", "variable") if key in pin}
            for name, pin in (pins or {}).items()}
