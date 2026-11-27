"""`qk pr check`: run a project's lanes on a GitHub pull request, merged with its base, and post each lane's result
as a commit status that a ruleset can require (vocaro/queuekeeper#375).

    qk pr check [NUMBER] [--lane LANE ...]

A project that integrates through pull requests has no merge queue to test its changes, and Queuekeeper's rule is that
the owner's Macs, not a hosted runner, run its checks. This command is that check:

- It asks `gh` for the pull request (the current branch's when no number is given): its head commit and its base.
- It fetches both and merges the head into the base in a checkout of its own under the repository's Git directory,
  kept per pull request so that its builds stay warm. What lands is that merge, so that is what the lanes test.
- Each lane runs as `qk run LANE` in that checkout, so it takes admission and placement as any lane does, with the
  merged tree's own queuekeeper.json, as the merge queue runs a batch's.
- Before each lane it posts `pending` on the head commit under the context `queuekeeper/LANE`, and after it `success`
  or `failure`. A run with no verdict, such as an unmeasured one or one the Mac had no room for, leaves the status
  pending, never success, so a required check holds the merge until a run gives a verdict.
- A head that conflicts with its base fails every lane's status, since nothing that would land can be tested.

Statuses are posted on the commit tested, so a push after the check started leaves the new head with no status of
this run's. Each pull request's check runs once at a time: a second run while one runs exits 75, as a held command
does."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import config as configuration, lane_results

CONTEXT = "queuekeeper/{}"
# GitHub refuses a status description longer than this.
DESCRIPTION_LIMIT = 140
# Under the repository's common Git directory, one checkout per pull request.
FOLDER = "queuekeeper-pr-checks"
FAILED, USAGE, WAITING = 1, 2, 75
# The merge is Queuekeeper's own, never pushed, so it needs an identity whether or not the person has set one.
IDENTITY = {"GIT_AUTHOR_NAME": "Queuekeeper", "GIT_AUTHOR_EMAIL": "queuekeeper@localhost",
            "GIT_COMMITTER_NAME": "Queuekeeper", "GIT_COMMITTER_EMAIL": "queuekeeper@localhost"}


def say(line):
    print(f"qk pr check: {line}", flush=True)


def no_verdict():
    """Every exit status of qk run that gives no verdict on the commit, as the merge queue counts them for its reruns:
    unmeasured, no room, a Mac lost, not started; and 75, a held or waiting command."""
    from .merge_queue import UNITTEST_RERUN_NO_VERDICT
    return UNITTEST_RERUN_NO_VERDICT | {WAITING}


def run(command, directory, env=None):
    """(status, standard output stripped, standard error stripped) of command in directory."""
    try:
        done = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=600,
                              env={**os.environ, **(env or {})})
    except (OSError, subprocess.SubprocessError) as error: return 127, "", str(error)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def git(directory, *words, env=None):
    return run(["git", "-C", str(directory), *words], directory, env)


def pull_request(root, number):
    """The pull request's number, head commit, base branch and URL, from gh, or raise ValueError saying why not."""
    status, out, err = run(["gh", "pr", "view", *([str(number)] if number else []), "--json",
                            "number,headRefOid,baseRefName,url,state"], root)
    if status: raise ValueError(f"gh could not find the pull request: {err or out or f'exit {status}'}")
    try: found = json.loads(out)
    except ValueError as error: raise ValueError(f"gh answered with something other than JSON: {error}") from error
    if not all(found.get(key) for key in ("number", "headRefOid", "baseRefName")):
        raise ValueError(f"gh's answer names no head commit or base: {out[:200]}")
    return found


def for_queue(root, number, label):
    """Pull request number of a pull-request-queue project as `qk land --pr` queues it, once its head is fetched:
    {number, head, title, headRefName, url, label}. Raises ValueError unless it is open, not a draft, based on main
    and labeled label, the person's approval (vocaro/queuekeeper#376)."""
    status, out, err = run(["gh", "pr", "view", str(number), "--json",
                            "number,state,isDraft,baseRefName,headRefOid,headRefName,title,labels,url"], root)
    if status: raise ValueError(f"gh could not find pull request {number}: {err or out or f'exit {status}'}")
    try: found = json.loads(out)
    except ValueError as error: raise ValueError(f"gh answered with something other than JSON: {error}") from error
    if found.get("state") != "OPEN":
        raise ValueError(f"pull request {number} is {str(found.get('state')).lower()}, so there is nothing to queue")
    if found.get("isDraft"): raise ValueError(f"pull request {number} is a draft: mark it ready for review first")
    if found.get("baseRefName") != "main":
        raise ValueError(f"pull request {number} targets {found.get('baseRefName')}, and the queue lands only on main")
    if label not in {item.get("name") for item in found.get("labels") or [] if isinstance(item, dict)}:
        raise ValueError(f"pull request {number} has no {label} label, by which a person approves it for the queue: "
                         f"add the label once it is reviewed")
    head = found.get("headRefOid")
    if not head: raise ValueError(f"gh's answer names no head commit for pull request {number}")
    status, _, err = git(root, "fetch", "-q", "origin", f"refs/pull/{number}/head")
    if status: raise ValueError(f"could not fetch pull request {number} from origin: {err}")
    if git(root, "cat-file", "-e", f"{head}^{{commit}}")[0]:
        raise ValueError(f"origin has no commit {head[:12]}, pull request {number}'s head: it was pushed over since gh "
                         "was asked, so land it again")
    return {"number": found["number"], "head": head, "title": found.get("title") or "",
            "headRefName": found.get("headRefName") or "", "url": found.get("url") or "", "label": label}


def labeled(root, label):
    """The open pull requests on main that carry label, as for_queue returns them less their fetch: a list, or
    raise ValueError when gh cannot list them. A draft is left out, as for_queue refuses it."""
    status, out, err = run(["gh", "pr", "list", "--state", "open", "--base", "main", "--label", label, "--limit", "100",
                            "--json", "number,headRefOid,headRefName,title,url,isDraft"], root)
    if status: raise ValueError(f"gh could not list the pull requests labeled {label}: {err or out or f'exit {status}'}")
    try: found = json.loads(out)
    except ValueError as error: raise ValueError(f"gh answered with something other than JSON: {error}") from error
    if not isinstance(found, list): raise ValueError("gh's list of pull requests is not a list")
    return [{"number": item["number"], "head": item["headRefOid"], "title": item.get("title") or "",
             "headRefName": item.get("headRefName") or "", "url": item.get("url") or "", "label": label}
            for item in found if isinstance(item, dict) and item.get("number") and item.get("headRefOid")
            and not item.get("isDraft")]


def recheck(root, pull_request, head):
    """Whether the queue may still push pull_request, queued at head, once its batch passed: (True, None); (False,
    why) when it was closed, its head moved or its label was taken off while it waited; or (None, why) when gh could
    not say, and nothing may be pushed until it can (vocaro/queuekeeper#376)."""
    number, label = pull_request["number"], pull_request.get("label") or "queue"
    status, out, err = run(["gh", "pr", "view", str(number), "--json", "state,headRefOid,labels"], root)
    try: found = json.loads(out) if not status else None
    except ValueError: found = None
    if not isinstance(found, dict):
        return None, f"gh could not say whether pull request {number} is still labeled {label}: {err or out or status}"
    if found.get("state") != "OPEN":
        return False, f"pull request {number} was {str(found.get('state')).lower()} while it waited in the queue"
    if found.get("headRefOid") != head:
        return False, (f"pull request {number} was pushed again while it waited, to {str(found.get('headRefOid'))[:12]}, "
                       f"so what passed is not its head: queue it again with qk land --pr {number}")
    if label not in {item.get("name") for item in found.get("labels") or [] if isinstance(item, dict)}:
        return False, f"pull request {number}'s {label} label was taken off while it waited in the queue"
    return True, None


def merge_message(pull_request):
    """The message of the merge commit by which the queue lands a pull request, which GitHub reads as its merge."""
    title = pull_request.get("title", "").strip()
    return (f"Merge pull request #{pull_request['number']} from {pull_request.get('headRefName') or 'its branch'}"
            + (f"\n\n{title}" if title else ""))


def post(root, sha, lane, state, description):
    """Post a commit status on sha through gh, and say so. Returns whether GitHub took it."""
    if len(description) > DESCRIPTION_LIMIT: description = description[:DESCRIPTION_LIMIT - 1] + "…"
    status, out, err = run(["gh", "api", "--method", "POST", f"repos/{{owner}}/{{repo}}/statuses/{sha}",
                            "-f", f"state={state}", "-f", f"context={CONTEXT.format(lane)}",
                            "-f", f"description={description}"], root)
    if status:
        print(f"qk pr check: could not post {state} for {lane} on {sha[:12]}: {err or out or f'exit {status}'}",
              file=sys.stderr)
        return False
    say(f"posted {state} for {CONTEXT.format(lane)} on {sha[:12]}: {description}")
    return True


def common_dir(root):
    status, out, err = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if status: raise ValueError(f"{root} is not in a Git checkout: {err}")
    return Path(out)


def checkout_for(root, number):
    """The checkout kept for pull request number, made on first use."""
    path = common_dir(root) / FOLDER / str(number)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        status, _, err = git(root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
        if status: raise ValueError(f"could not make the pull request's checkout at {path}: {err}")
    return path


def merged(root, checkout, request):
    """The merge of the pull request's head into its base, checked out in checkout: (commit, None), or (None, why
    it conflicts). Raises ValueError when the commits cannot be fetched."""
    base, head, number = request["baseRefName"], request["headRefOid"], request["number"]
    status, _, err = git(root, "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
                         f"refs/pull/{number}/head")
    if status: raise ValueError(f"could not fetch {base} and pull request {number} from origin: {err}")
    if git(root, "cat-file", "-e", f"{head}^{{commit}}")[0]:
        raise ValueError(f"origin has no commit {head[:12]}, the pull request's head: it was pushed over since gh "
                         "was asked, so check again")
    for words in (("reset", "-q", "--hard"), ("clean", "-fdq"), ("checkout", "-q", "--detach", f"origin/{base}")):
        status, _, err = git(checkout, *words)
        if status: raise ValueError(f"could not prepare {checkout}: git {words[0]}: {err}")
    status, out, err = git(checkout, "merge", "-q", "--no-ff", "--no-edit", "-m",
                           f"Merge pull request #{number} for qk pr check", head, env=IDENTITY)
    if status:
        git(checkout, "merge", "--abort")
        return None, f"pull request {number} conflicts with {base}: {(err or out).splitlines()[-1] if err or out else ''}"
    return git(checkout, "rev-parse", "HEAD")[1], None


def run_lane(checkout, lane):
    """The exit status of `qk run LANE` in checkout, its output passed through."""
    try: return subprocess.run(configuration.toolkit_command(checkout, ["run", lane]), cwd=checkout).returncode
    except OSError as error:
        print(f"qk pr check: could not start qk run {lane}: {error}", file=sys.stderr)
        return lane_results.NOT_STARTED


def lanes_of(checkout, named):
    """The lanes to run: those named, else gate. Raises ValueError for a lane the merged tree does not have."""
    try: declared = configuration.load(checkout).get("lanes", {})
    except (OSError, ValueError) as error: raise ValueError(f"the merged tree's queuekeeper.json: {error}") from error
    lanes = named or ["gate"]
    missing = [lane for lane in lanes if lane not in declared]
    if missing and not named:
        raise ValueError("the project has no gate lane: name the lanes to check with --lane LANE")
    if missing: raise ValueError(f"the merged tree has no lane {', '.join(missing)}")
    return lanes


def elapsed(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def check(root, number, named):
    request = pull_request(root, number)
    number, head = request["number"], request["headRefOid"]
    if request.get("state") not in (None, "OPEN"):
        say(f"pull request {number} is {request['state'].lower()}, so nothing is checked")
        return 0
    checkout = checkout_for(root, number)
    with open(checkout.parent / f"{number}.lock", "w") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"qk pr check: pull request {number} is being checked already; wait for that run", file=sys.stderr)
            return WAITING
        say(f"pull request {number} ({request.get('url') or 'no URL'}): head {head[:12]} into {request['baseRefName']}")
        commit, conflict = merged(root, checkout, request)
        if conflict:
            for lane in named or ["gate"]: post(root, head, lane, "failure", conflict)
            say(conflict)
            return FAILED
        say(f"testing the merge {commit[:12]} in {checkout}")
        lanes = lanes_of(checkout, named)
        worst = 0
        for lane in lanes:
            post(root, head, lane, "pending", f"{lane} is running on the owner's Macs")
            began = time.monotonic()
            status = run_lane(checkout, lane)
            took = elapsed(time.monotonic() - began)
            if status == 0:
                post(root, head, lane, "success", f"{lane} passed in {took} on the merge with {request['baseRefName']}")
            elif status < 0 or status in no_verdict():
                # Pending, never success: a required check holds the merge until a run gives a verdict.
                post(root, head, lane, "pending", f"{lane} gave no verdict (exit {status}) after {took}: run "
                     "qk pr check again")
                worst = worst or WAITING
            else:
                post(root, head, lane, "failure", f"{lane} failed (exit {status}) after {took}; its log is on the "
                     "Mac that ran it")
                worst = FAILED
        return worst


def main(root, argv):
    """`qk pr check`."""
    parser = argparse.ArgumentParser(prog="qk pr", description="Check a GitHub pull request on this Mac's lanes and "
                                     "post each lane's result as a commit status")
    actions = parser.add_subparsers(dest="action", required=True)
    checked = actions.add_parser("check", help="run lanes on the pull request merged with its base, posting "
                                 "queuekeeper/LANE statuses on its head commit")
    checked.add_argument("number", nargs="?", type=int, help="the pull request; the current branch's without it")
    checked.add_argument("--lane", action="append", default=[], metavar="LANE",
                         help="a lane to run; repeat for more (default gate)")
    actions.add_parser("queue", help="in a pull-request-queue project, queue each open pull request on main that "
                       "carries the queue's label and is not queued at its head yet, as the queue's launcher does "
                       "every two minutes")
    args = parser.parse_args(argv)
    status, top, err = git(root, "rev-parse", "--show-toplevel")
    if status:
        print(f"qk pr check: runs in a Git checkout: {err}", file=sys.stderr)
        return USAGE
    if args.action == "queue":
        from .merge_queue import queue_labeled
        try: return queue_labeled(Path(top), say=say, quiet=False)
        except ValueError as error:
            print(f"qk pr queue: {error}", file=sys.stderr)
            return FAILED
    try: return check(Path(top), args.number, args.lane)
    except ValueError as error:
        print(f"qk pr check: {error}", file=sys.stderr)
        return FAILED
