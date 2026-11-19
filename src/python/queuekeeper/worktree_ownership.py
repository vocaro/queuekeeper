"""Durable proof of who made a disposable worktree, and recovery of one its maker left behind (vocaro/queuekeeper#138).

qk land's scratch worktrees (merge_queue.scratch) are locked, so that no sweep takes one while it is used, and
removed when the block that made them ends. A process killed inside that block never removes its worktree, and the
lock then keeps it for good: a lock's reason says what the worktree was for, never who made it or whether that
process is gone. So each scratch worktree has a record, written before the worktree is added, under the
repository's queue folder (FOLDER): its repository, path and folder, this Mac's hardware UUID, the PID and start of
the process that made it, and what it is for. Its lock's reason names the record, so that the record and the
registration prove each other.

`reclaim` removes a scratch worktree only when all of these hold, and otherwise keeps it, saying why:

- its record is whole, names this repository and was written on this Mac;
- its maker has ended: its PID runs no more, or runs a process with another start (a PID reused); a maker whose
  state cannot be read counts as running;
- the registration at the record's path is in this repository and its lock names the record;
- no process has its working directory, an open file or an argument inside it (disk.census; a census that
  fails keeps it);
- it holds no change, untracked or ignored file and no operation in progress (a cherry-pick, a merge);
- every commit of its HEAD that no ref holds has an equal patch on origin/main.

It is then unlocked and removed without --force, which refuses a worktree with changes, and its folder is removed
only when nothing else is left in it: a preflight lane's log there is evidence. A registration whose directory has
gone is forgotten the same way, by removing exactly its own administrative folder, once its HEAD is proven landed.
A record whose worktree was never added, or was removed, is dropped.

A worktree locked with the scratch reason but naming no record was made before records were kept: its maker is
unknown, so `legacy` lists it and nothing removes it. A path prefix or a lock's words alone is never authority to
delete.
"""
import contextlib
import fcntl
import functools
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time

from . import disk, offload_host, resources

SCHEMA = 1
# Under the repository's common Git directory, beside the queue's entries.
FOLDER = "queuekeeper-queue/scratch"
# What a scratch worktree's lock reason ends with: the record that proves who made it.
MARK = "queuekeeper scratch record"
# The scratch reasons of the versions that kept no record, whose worktrees `legacy` lists and nothing removes.
LEGACY_REASONS = ("qk land's trial integration of a new entry, removed when it ends",
                  "eng land's trial integration of a new entry, removed when it ends")
MAIN = "refs/remotes/origin/main"
GIT_TIMEOUT = 600
# What a worktree's own Git directory holds while an operation is under way.
IN_PROGRESS = ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply")

RECLAIMED, FORGOTTEN, KEPT = "reclaimed", "forgotten", "kept"


def run_git(directory, *arguments, check=False, input=None):
    """Git in directory with no hook, and without what a hook's environment would aim at another checkout."""
    local = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split()
    env = {key: value for key, value in os.environ.items() if key not in set(local)}
    done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *arguments],
                          capture_output=True, text=True, env=env, timeout=GIT_TIMEOUT,
                          **({"input": input} if input is not None else {"stdin": subprocess.DEVNULL}))
    if check and done.returncode:
        raise RuntimeError(f"`git {' '.join(arguments[:2])}` exited {done.returncode}: "
                           f"{done.stderr.strip() or done.stdout.strip() or 'no output'}")
    return done


def common_dir(root, git=run_git):
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", check=True).stdout.strip())


def records(common):
    return Path(common) / FOLDER


@functools.cache
def this_mac():
    """This Mac's hardware UUID, which stays when its name changes, or its host name when the UUID cannot be read."""
    return offload_host.hardware_id() or f"host:{socket.gethostname()}"


def collapsed(start):
    return " ".join(str(start).split())


def maker():
    """The PID and start of this process, which a record names as the worktree's maker."""
    try: start = resources.process_start(os.getpid())
    except OSError: start = None
    return {"pid": os.getpid(), "start": collapsed(start) if start else None}


def canonical(path):
    return os.path.realpath(str(path))


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(staged, path)


@contextlib.contextmanager
def scratch(root, base, kind, reason, name, git=run_git):
    """A detached worktree of root's repository at base, at FOLDER/name in a new folder outside every checkout,
    locked with reason and the record that proves this process made it; it goes, and its record, when the block
    ends. The record is written before the worktree is added, so that a maker killed at any point leaves a record
    for every worktree it may have left."""
    common = common_dir(root, git)
    folder = Path(canonical(tempfile.mkdtemp(prefix=f"qk-land-{kind}-")))
    path = folder / name
    identifier = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(4)}"
    record_path = records(common) / f"{identifier}.json"
    try:
        write_record(record_path, {"schemaVersion": SCHEMA, "id": identifier, "repository": canonical(common),
                                   "path": str(path), "folder": str(folder), "host": this_mac(),
                                   "hostName": socket.gethostname(), **maker(), "purpose": kind, "reason": reason,
                                   "base": base, "created": time.time()})
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    try:
        git(root, "worktree", "add", "--detach", "--lock", "--reason", f"{reason} [{MARK} {identifier}]", str(path),
            base, check=True)
        yield path
    finally:
        # Unlocked first, so that a worktree the removal leaves registered is one Git's own pruning takes.
        git(root, "worktree", "unlock", str(path))
        git(root, "worktree", "remove", "--force", "--force", str(path))
        shutil.rmtree(folder, ignore_errors=True)
        # Kept while Git still lists the worktree, so that reclaim can prove whose it is.
        if registration(common, path) is None and not os.path.lexists(path):
            with contextlib.suppress(OSError): record_path.unlink()


def registrations(common):
    """Each linked worktree registered in common's repository, from its administrative folder, as a dict: path (the
    worktree's own folder, as its gitdir file names it), admin (the administrative folder), reason (its lock's
    reason, or None when it is not locked) and head (what its HEAD file holds). Read without Git, so that a
    registration whose folder has gone reads as one that is there."""
    found = []
    try: admins = sorted((Path(common) / "worktrees").iterdir())
    except OSError: return found
    for admin in admins:
        try: gitdir = (admin / "gitdir").read_text().strip()
        except OSError: continue
        try: reason = (admin / "locked").read_text().strip()
        except FileNotFoundError: reason = None
        except OSError: reason = ""
        try: head = (admin / "HEAD").read_text().strip()
        except OSError: head = ""
        found.append({"path": os.path.dirname(gitdir), "admin": admin, "reason": reason, "head": head})
    return found


def registration(common, path):
    """The registration of the worktree at path, or None."""
    wanted = canonical(path)
    return next((each for each in registrations(common) if canonical(each["path"]) == wanted), None)


def maker_state(record):
    """running, ended (its PID runs no more, or runs another process: a PID reused) or unknown, which keeps."""
    pid, start = record.get("pid"), record.get("start")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(start, str) or not start:
        return "unknown"
    try: os.kill(pid, 0)
    except ProcessLookupError: return "ended"
    except PermissionError: pass
    except OSError: return "unknown"
    try: now = resources.process_start(pid)
    except OSError: return "unknown"
    if now is None:
        try: os.kill(pid, 0)
        except ProcessLookupError: return "ended"
        except OSError: pass
        return "unknown"
    return "running" if collapsed(now) == collapsed(start) else "ended"


def unlanded(root, head, git=run_git):
    """Why head's history is not safe to drop, or None: each commit of it that no ref holds must have an equal patch
    on origin/main. Worktrees' own HEADs are not refs, so a scratch worktree's HEAD does not prove itself."""
    if not head: return "its HEAD cannot be read"
    if head.startswith("ref: "): return None if git(root, "rev-parse", "--verify", "--quiet", head[5:]).returncode == 0 \
        else f"its HEAD names {head[5:]}, which cannot be read"
    refs = git(root, "for-each-ref", "--format=%(objectname)")
    if refs.returncode: return "the repository's refs cannot be read"
    listed = git(root, "rev-list", "--stdin", input="\n".join([head, *(f"^{ref}" for ref in set(refs.stdout.split()))]) + "\n")
    if listed.returncode: return f"its HEAD {head[:12]} cannot be read: {listed.stderr.strip() or 'no output'}"
    own = listed.stdout.split()
    if not own: return None
    compared = git(root, "cherry", MAIN, head)
    if compared.returncode: return f"its commits cannot be compared with origin/main: {compared.stderr.strip() or 'no output'}"
    missing = {line[2:].strip() for line in compared.stdout.splitlines() if line.startswith("+ ")}
    left = [commit for commit in own if commit in missing]
    if left:
        return (f"{len(left)} commit(s) of its HEAD, {left[0][:12]} first, are on no ref and have no equal patch on "
                "origin/main")
    return None


def worktree_state(path, git=run_git):
    """Why the worktree at path holds something to keep, or None when it is clean, with no untracked or ignored file
    and no operation under way."""
    status = git(path, "status", "--porcelain=v1", "--ignored", "--untracked-files=all")
    if status.returncode: return f"its status cannot be read: {status.stderr.strip() or 'no output'}"
    if status.stdout.strip():
        first = status.stdout.splitlines()[0]
        return f"it holds changes, untracked or ignored files, such as {first[3:]}"
    for name in IN_PROGRESS:
        found = git(path, "rev-parse", "--git-path", name)
        if found.returncode: return "whether an operation is under way cannot be read"
        named = Path(found.stdout.strip())
        if not named.is_absolute(): named = Path(path) / named
        if named.exists(): return f"an operation is under way ({name})"
    return None


def read_record(path):
    try: record = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error: return None, f"its record cannot be read: {error}"
    needed = ("id", "repository", "path", "folder", "host", "pid", "start")
    if not isinstance(record, dict) or record.get("schemaVersion") != SCHEMA \
            or any(not record.get(key) for key in needed):
        return None, "its record is not whole"
    if record["id"] != Path(path).stem: return None, "its record names another ID than its file"
    return record, None


class Census:
    """One census of the account's processes, taken when first asked for, or the error that stopped it."""
    def __init__(self, take=disk.census):
        self.take, self.processes, self.error = take, None, None

    def users(self, *paths):
        if self.processes is None and self.error is None:
            try: self.processes = self.take()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error: self.error = error
        if self.error is not None: raise RuntimeError(f"whether a process uses it is unknown: {self.error}")
        found = []
        for path in paths:
            found += [process for process in disk.users(self.processes, canonical(path)) if process not in found]
        return found


def outcome(record_path, record, state, why, **more):
    return {"record": str(record_path), "id": (record or {}).get("id") or Path(record_path).stem,
            "path": (record or {}).get("path"), "purpose": (record or {}).get("purpose"),
            "outcome": state, "why": why, **more}


def reclaim(root, *, dry_run=False, census=None, git=run_git, quiet_running=False):
    """Recover the scratch worktrees whose makers have ended, as this module's docstring says, and return what became
    of each record, as outcome dicts. With dry_run, nothing changes and "outcome" says what would. One recovery runs
    at a time in a repository; another finds none to do. With quiet_running, a record whose maker still runs, the
    usual case, is left out."""
    common = common_dir(root, git)
    folder = records(common)
    try: paths = sorted(folder.glob("*.json"))
    except OSError: return []
    if not paths: return []
    census = census or Census()
    results = []
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return []
        for path in sorted(folder.glob("*.json")):
            result = reclaim_one(root, common, path, dry_run, census, git)
            if quiet_running and result.get("maker") == "running": continue
            results.append(result)
    return results


def reclaim_one(root, common, record_path, dry_run, census, git):
    record, why = read_record(record_path)
    if record is None: return outcome(record_path, None, KEPT, why)
    keep = lambda why, **more: outcome(record_path, record, KEPT, why, **more)
    if canonical(record["repository"]) != canonical(common): return keep("its record names another repository")
    if record["host"] != this_mac(): return keep(f"it was made on another Mac ({record.get('hostName') or record['host']})")
    state = maker_state(record)
    if state == "running": return keep(f"its maker, PID {record['pid']}, still runs", maker=state)
    if state != "ended": return keep(f"whether its maker, PID {record['pid']}, still runs is unknown", maker=state)
    path, folder = Path(record["path"]), Path(record["folder"])
    if canonical(path.parent) != canonical(folder): return keep("its record's path is not inside its folder")
    registered = registration(common, path)
    if registered is None:
        if os.path.lexists(path):
            return keep(f"{path} is there but is no worktree of this repository, so nothing proves whose it is")
        # Never added, or removed before its record was: nothing of it is left but, perhaps, its empty folder.
        if not dry_run:
            with contextlib.suppress(OSError): folder.rmdir()
            with contextlib.suppress(OSError): record_path.unlink()
        return outcome(record_path, record, FORGOTTEN, "its maker ended, and no worktree of it is registered or there",
                       maker=state)
    if not registered["reason"] or f"[{MARK} {record['id']}]" not in registered["reason"]:
        return keep("its registration's lock does not name this record")
    admin = Path(registered["admin"])
    if canonical(admin.parent) != canonical(Path(common) / "worktrees"): return keep("its registration is elsewhere")
    if not os.path.lexists(path):
        why = unlanded(root, registered["head"], git)
        if why: return keep(why)
        if not dry_run:
            # Exactly this registration: what `git worktree prune` would remove for it once unlocked.
            again = registration(common, path)
            if again is None or Path(again["admin"]) != admin or os.path.lexists(path):
                return keep("its registration changed while it was checked")
            shutil.rmtree(admin)
            with contextlib.suppress(OSError): folder.rmdir()
            with contextlib.suppress(OSError): record_path.unlink()
        return outcome(record_path, record, RECLAIMED, "its maker ended and its folder had gone; its registration "
                       "is forgotten", maker=state)
    why = worktree_state(path, git)
    if why: return keep(why)
    head = git(path, "rev-parse", "HEAD")
    why = unlanded(root, head.stdout.strip() if head.returncode == 0 else "", git)
    if why: return keep(why)
    try: users = census.users(path, folder)
    except RuntimeError as error: return keep(str(error))
    if users: return keep(disk.described((users[0], "working directory, an open file or an argument")))
    if dry_run: return outcome(record_path, record, RECLAIMED, "its maker ended and nothing uses it", maker=state)
    git(root, "worktree", "unlock", str(path))
    removed = git(root, "worktree", "remove", str(path))
    if removed.returncode:
        git(root, "worktree", "lock", "--reason", registered["reason"], str(path))
        return keep(f"`git worktree remove` failed: {removed.stderr.strip() or 'no output'}")
    left = []
    try: left = sorted(entry.name for entry in folder.iterdir())
    except FileNotFoundError: pass
    except OSError: left = ["?"]
    if not left:
        with contextlib.suppress(OSError): folder.rmdir()
    with contextlib.suppress(OSError): record_path.unlink()
    return outcome(record_path, record, RECLAIMED, "its maker ended and nothing uses it"
                   + (f"; {folder} stays, since it holds {', '.join(left)}" if left else ""), maker=state)


def legacy(root, git=run_git):
    """The scratch worktrees made before records were kept, which nothing removes: their maker is unknown."""
    common = common_dir(root, git)
    return [{"path": each["path"], "reason": each["reason"], "head": each["head"],
             "missing": not os.path.lexists(each["path"]),
             "why": "made before scratch worktrees had records, so its maker is unknown: check it by hand"}
            for each in registrations(common)
            if each["reason"] and MARK not in each["reason"]
            and any(each["reason"].startswith(reason) for reason in LEGACY_REASONS)]


def forget_missing(root, path, reasons, git=run_git):
    """Forget the registration of the worktree at path, whose folder has gone, when it is locked with one of reasons
    and its HEAD is proven landed (unlanded): exactly its own administrative folder goes, as `git worktree prune`
    would remove it once unlocked, and no other registration. None when it went, else why it stays."""
    common = common_dir(root, git)
    found = registration(common, path)
    if found is None: return "no worktree is registered there"
    if os.path.lexists(path): return f"{path} is still there"
    if found["reason"] not in reasons: return "its lock does not name the checkout expected there"
    admin = Path(found["admin"])
    if canonical(admin.parent) != canonical(common / "worktrees"): return "its registration is elsewhere"
    why = unlanded(root, found["head"], git)
    if why: return why
    shutil.rmtree(admin)
    return None


def words(result):
    where = result.get("path") or result["record"]
    return f"{result['outcome']} {where}: {result['why']}"
