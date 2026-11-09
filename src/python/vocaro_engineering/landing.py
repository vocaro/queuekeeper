"""Whether a commit's work is in a base branch, and whether the last ref to it may go: the proofs
that Closeout's landed.sh (skills/closeout/scripts) and `eng disk` share, so that they never
disagree about the same commits.

When main holds a branch's work only as equal patches, a squash, a pull request or commits that the
merge queue rebased past a conflict it settled by keeping both sides (resolved), the branch is
the last ref to its own commits, and deleting it leaves them to git gc. Evidence names commits by
their IDs, so such a ref stays while main's tree or the project's open issues cite one of its
commits, while an open issue names the branch, or while either could not be read.

Such a ref can go once a merge that brings no content puts its commits in main's history. Every
commit the merge brings in then reaches main with its message, and GitHub acts on each closing
keyword in it, so retire.sh refuses the merge unless each issue those commits claim to close is
already claimed by a commit main holds and is closed now.

landed.sh adds what needs the network: a merged pull request, and the open issues, read with gh.
`eng disk` never fetches and reads neither. landed.sh and retire.sh run this file as a script:

    python3 -I landing.py contained BASE TIP     (the method, for resolved with what it rests on)
    python3 -I landing.py citations TIP --base REF [--base REF] [--branch NAME] [--issues]
    python3 -I landing.py claims BASE TIP...

It imports nothing from the toolkit, so it runs isolated from the checkout it examines, and it
keeps to Python 3.9, the second Mac's /usr/bin/python3.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ANCESTOR, PATCH_EQUIVALENT, CONTENT_MERGED, RESOLVED = "ancestor", "patch-equivalent", "content-merged", "resolved"
# A commit is cited by a prefix of its ID: 7 to 40 hexadecimal digits that stand as a word.
CITATION = r"[0-9a-fA-F]{7,40}"
WORD = re.compile(rf"(?<![0-9A-Za-z_]){CITATION}(?![0-9A-Za-z_])")
# Open issues one lookup reads; a project with as many has more, so its lookup is incomplete.
ISSUE_LIMIT = 1000
GH_TIMEOUT = 60
# A file in which the sweep's first lookup leaves the open issues, or why they could not be read,
# for every later one in the same sweep.
ISSUES_CACHE = "CLOSEOUT_ISSUES_CACHE"
# A closing keyword GitHub acts on when a commit reaches the default branch: close, fix or resolve,
# in any tense, before an issue here (#N), in another repository (owner/name#N) or its URL.
CLAIM = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
                   r"(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)(\d+)\b", re.I)
# A claim GitHub acts on although its sentence denies it (vocaro-engineering#286): one of these within NEGATED_WITHIN
# words before the keyword, in the same clause. "not only fixes #N" asserts the fix, so it is no negation.
NEGATION = re.compile(r"\b(?:(?:can)?not\b(?!\s+(?:only|just|merely|simply)\b)|never\b|without\b|no\s+longer\b)"
                      r"|(?<=[a-z])n['\u2019]t\b", re.I)
NEGATED_WITHIN = 3
# Where a clause ends: its punctuation, a conjunction that starts another, a blank line, a list item, or a line that
# opens with a capital, as a new sentence or trailer does where a wrapped line would go on in lower case. "or" joins
# what one negation governs ("not fix or close #N"), and "yet" stays inside it ("not yet fix #N").
CLAUSE_END = re.compile(r"[,;:()\u2013\u2014]|[.!?](?=\s|$)|\s-\s|\n\s*(?:\n|[*+]\s|(?=[A-Z]))"
                        r"|(?i:\b(?:and|but|so|while|because|since|although|though)\b)")


def contained(git, base, tip):
    """How commit tip is in base, or None when nothing here proves it:

    - ancestor: tip is reachable from base (a merge or a fast-forward).
    - patch-equivalent: every commit has one with the same diff in base (a rebase or a
      cherry-pick), and no merge brings in what base lacks (own_merges), whose own change a patch
      comparison never sees.
    - content-merged: merging tip into base would change nothing (a squash). A squash that base
      has since edited over proves none of these.
    - resolved: every commit has an equal patch in base, or one that a rebase moved past a
      conflict it settled by keeping both sides (see resolutions).

    git(*arguments) runs git in the repository and returns its CompletedProcess."""
    return proof(git, base, tip)[0]


def proof(git, base, tip):
    """(how, what it rests on) for contained: the second is empty but for resolved, where it names
    each commit, the commit base holds it as, and the files where only the lines around differ."""
    if git("merge-base", "--is-ancestor", tip, base).returncode == 0: return ANCESTOR, ""
    merges = own_merges(git, base, tip)
    # git cherry passes over merges: pending is the commits of tip's own that base has no patch of.
    cherry = git("cherry", base, tip)
    if merges is None or cherry.returncode: return None, ""
    pending = [line.split()[1] for line in cherry.stdout.splitlines() if line.startswith("+")]
    if not merges and not pending: return PATCH_EQUIVALENT, ""
    merged = git("merge-tree", "--write-tree", base, tip)
    tree = git("rev-parse", f"{base}^{{tree}}")
    if merged.returncode == 0 and tree.returncode == 0 and merged.stdout.split("\n", 1)[0] == tree.stdout.strip():
        return CONTENT_MERGED, ""
    if merges: return None, ""
    found = resolutions(git, base, tip, pending)
    if not found: return None, ""
    return RESOLVED, "; ".join(f"{commit[:12]} as {landed[:12]}, whose patch differs only in where its lines sit in "
                               + ", ".join(paths) for commit, landed, paths in found)


def own_merges(git, base, tip):
    """The merges in base..tip that bring in work of their own, or None when git could not say. A merge
    brings none when base holds each of its parents but its first and its tree is the one a clean merge
    of its parents makes: a branch refreshed by merging main into it, as the desktop app's sync and a
    `git merge origin/main` do. Its branch's work is then its other commits, which the merge queue lands
    replayed onto main (merge_queue.linear), so equal patches prove it landed."""
    listed = git("rev-list", "--merges", f"{base}..{tip}")
    if listed.returncode: return None
    found = []
    for merge in listed.stdout.split():
        parents = git("rev-list", "--parents", "-n", "1", merge)
        if parents.returncode: return None
        others = parents.stdout.split()[2:]
        reached = [git("merge-base", "--is-ancestor", parent, base).returncode for parent in others]
        if any(code not in (0, 1) for code in reached): return None
        if any(reached):
            found.append(merge)
            continue
        # A merge that settled a conflict, or added a change of its own, has a tree no clean merge makes.
        clean = git("merge-tree", "--write-tree", *parents.stdout.split()[1:])
        tree = git("rev-parse", f"{merge}^{{tree}}")
        if tree.returncode: return None
        if clean.returncode or clean.stdout.split("\n", 1)[0] != tree.stdout.strip(): found.append(merge)
    return found


def brought_in(git, base, merges):
    """[(commit, subject)] for what merges, newest first as own_merges lists them, bring in beside their first
    parents: the commits, oldest first, that base has no patch of, each once, or None when git could not say. A merge
    within what they bring in is not listed, since `git cherry` passes over merges."""
    found = {}
    for merge in reversed(merges):
        parents = git("rev-list", "--parents", "-n", "1", merge)
        if parents.returncode: return None
        first, *others = parents.stdout.split()[1:]
        for other in others:
            cherry = git("cherry", "-v", base, other, first)
            if cherry.returncode: return None
            for line in cherry.stdout.splitlines():
                mark, commit, *subject = line.split(" ", 2)
                if mark == "+": found.setdefault(commit, subject[0] if subject else "")
    return list(found.items())


def changes(git, commit):
    """{path: (status, old mode, new mode, old blob, new blob, binary)} for what a commit that is not
    a merge changes against its parent, or None when git could not say."""
    raw = git("diff-tree", "-r", "--no-renames", "--no-commit-id", "-z", "--raw", f"{commit}^", commit)
    counts = git("diff-tree", "-r", "--no-renames", "--no-commit-id", "-z", "--numstat", f"{commit}^", commit)
    if raw.returncode or counts.returncode: return None
    fields = raw.stdout.split("\0")
    found = {}
    for index in range(0, len(fields) - 1, 2):
        meta, path = fields[index].split(), fields[index + 1]
        if len(meta) != 5: return None
        found[path] = [meta[4], meta[0].lstrip(":"), meta[1], meta[2], meta[3], False]
    for entry in counts.stdout.split("\0"):
        added, _, rest = entry.partition("\t")
        removed, _, path = rest.partition("\t")
        if path in found: found[path][5] = added == "-" or removed == "-"
    return {path: tuple(entry) for path, entry in found.items()}


def hunks(git, commit, path):
    """The lines a commit adds and removes in path, hunk by hunk, with their signs and with nothing
    around them, or None when git could not say."""
    diff = git("--literal-pathspecs", "diff-tree", "-p", "-U0", "--no-renames", "--no-color", "--no-ext-diff",
               "--no-commit-id", f"{commit}^", commit, "--", path)
    if diff.returncode: return None
    found = None
    for line in diff.stdout.splitlines():
        if line.startswith("@@"): found = (found or []) + [[]]
        elif found is not None and line[:1] in ("+", "-", "\\"): found[-1].append(line)
    return found or []


def same_change(git, commit, landed):
    """The files where commit and landed, two commits that are not merges, add and remove the same
    lines but between different ones, or None when they differ in anything else: a file one of
    them changes and the other does not, a mode, a binary file, or a line either adds or removes."""
    mine, theirs = changes(git, commit), changes(git, landed)
    if mine is None or theirs is None or set(mine) != set(theirs): return None
    differing = []
    for path in sorted(mine):
        if mine[path] == theirs[path]: continue
        if mine[path][:3] != theirs[path][:3] or mine[path][5] or theirs[path][5]: return None
        own = hunks(git, commit, path)
        if own is None or own != hunks(git, landed, path): return None
        differing.append(path)
    return differing


def resolutions(git, base, tip, pending):
    """[(commit, landed, paths)] when each pending commit, one of tip's with no equal patch in base, is
    in base as landed: a commit base holds and tip does not, with the same subject and author, that
    adds and removes the same lines, file for file and hunk by hunk, and differs only in the lines
    around them in paths. Each landed commit stands for one pending commit. None when any is not.

    This is the merge queue's rebase of an entry past one ahead of it, settled by keeping both sides
    (two changelog entries, say; guides/maintenance.md): the branch's line then lands beside other
    neighbors, so its patch is not the branch's. Comparing each commit with its own landed copy, and
    not with base's tree, keeps it landed when later work in base rewrites a line it added."""
    if not pending: return None
    log = git("log", "--no-merges", "--format=%H%x00%ae%x00%s", base, f"^{tip}")
    if log.returncode: return None
    candidates = [line.split("\0") for line in log.stdout.splitlines() if line.count("\0") == 2]
    used, found = set(), []
    for commit in pending:
        shown = git("log", "-1", "--format=%ae%x00%s", commit)
        if shown.returncode: return None
        author, _, subject = shown.stdout.rstrip("\n").partition("\0")
        for landed, landed_author, landed_subject in candidates:
            if landed in used or (landed_author, landed_subject) != (author, subject): continue
            paths = same_change(git, commit, landed)
            if paths is not None:
                used.add(landed)
                found.append((commit, landed, paths))
                break
        else:
            return None
    return found


def cites(text, commits):
    """The commits that text names by a prefix of their IDs."""
    prefixes = {match.lower() for match in WORD.findall(text)}
    return [commit for commit in commits if any(commit[:length] in prefixes for length in range(7, 41))]


def short(ref):
    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix): return ref[len(prefix):]
    return ref


def tree_citations(git, commits, trees):
    """({commit: [record]}, whether the trees could be read), each record `PATH in TREE`."""
    grep = git("grep", "-I", "-o", "-w", "-E", CITATION, *trees, "--")
    found = {}
    for line in grep.stdout.splitlines():
        tree, _, rest = line.partition(":")
        path, _, token = rest.rpartition(":")
        for commit in cites(token, commits): found.setdefault(commit, []).append(f"{path} in {short(tree)}")
    return found, grep.returncode in (0, 1)  # 1: no match


def open_issues(cache=None):
    """(the project's open issues with their comments, None), or (None, why gh could not list them)."""
    if cache and os.path.exists(cache):
        with open(cache) as stored: saved = json.load(stored)
        return saved.get("issues"), saved.get("unmeasured")
    issues, why = None, None
    try:
        listed = subprocess.run(["gh", "issue", "list", "--state", "open", "--limit", str(ISSUE_LIMIT), "--json",
                                 "number,url,body,comments"], capture_output=True, text=True, timeout=GH_TIMEOUT,
                                stdin=subprocess.DEVNULL,
                                env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
        if listed.returncode:
            why = "gh issue list failed: " + (listed.stderr.strip().splitlines() or ["no message"])[0]
        else:
            issues = json.loads(listed.stdout)
            if len(issues) >= ISSUE_LIMIT: issues, why = None, f"the project has {ISSUE_LIMIT} or more open issues"
    except FileNotFoundError: why = "gh is not installed"
    except subprocess.TimeoutExpired: why = f"gh issue list took over {GH_TIMEOUT}s"
    except ValueError as error: why = f"gh issue list printed no issue list ({error})"
    if cache:
        with open(cache, "w") as stored: json.dump({"issues": issues, "unmeasured": why}, stored)
    return issues, why


def issue_citations(issues, commits, branch=None):
    """({commit: [record]}, [record that names branch]) for the open issues and their comments."""
    named = re.compile(rf"(?<![\w./-]){re.escape(branch)}(?![\w/-]|\.\w)") if branch else None
    found, naming = {}, []
    for issue in issues:
        records = [(f"issue #{issue['number']}", issue.get("body") or "")]
        records += [(f"a comment on issue #{issue['number']} ({comment.get('url', '')})", comment.get("body") or "")
                    for comment in issue.get("comments") or []]
        for record, text in records:
            for commit in cites(text, commits): found.setdefault(commit, []).append(record)
            if named and named.search(text): naming.append(record)
    return found, naming


def listed(records):
    unique = list(dict.fromkeys(records))
    return ", ".join(unique[:3]) + (f" and {len(unique) - 3} more" if len(unique) > 3 else "")


def citations(git, tip, bases, branch=None, issues=False, cache=None):
    """(report lines, retain) for the last ref to tip. retain says why the ref must stay, or is None
    when nothing a base does not reach is cited, by main's tree or (with issues) the project's open
    issues and their comments, and both could be read."""
    commits = git("rev-list", tip, *(f"^{base}" for base in bases))
    if commits.returncode: return [], "the commits that main does not reach could not be listed"
    commits = commits.stdout.split()
    if not commits: return [], None
    found, measured = tree_citations(git, commits, bases)
    unmeasured = [] if measured else ["git grep could not read main's tree"]
    naming = []
    if not issues:
        unmeasured.append("the project's open issues were not read, with no remote or with --no-fetch")
    else:
        open_now, why = open_issues(cache)
        if open_now is None: unmeasured.append(f"the project's open issues could not be read: {why}")
        else:
            cited, naming = issue_citations(open_now, commits, branch)
            for commit, records in cited.items(): found.setdefault(commit, []).extend(records)
    lines = [f"unreached: {len(commits)} commit(s) that no base reaches"]
    lines += [f"cited: {commit[:12]} by {listed(found[commit])}" for commit in commits if commit in found]
    if naming: lines.append(f"cited: the branch {branch} by {listed(naming)}")
    reasons = []
    if found: reasons.append(f"{len(found)} of its {len(commits)} commit(s) that main does not reach are cited")
    if naming: reasons.append("an open issue names the branch")
    if unmeasured: reasons.append("whether anything cites its commits is unknown: " + "; ".join(unmeasured))
    lines += [f"unmeasured: {reason}" for reason in unmeasured]
    return lines, "; ".join(reasons) or None


def claimed(git, *revisions):
    """{(repository, number): [commit]} for the closing claims in those commits' messages, with
    repository "" for this one, or None when the messages could not be read."""
    read = messages(git, *revisions)
    if read is None: return None
    found = {}
    for commit, message in read:
        for match in CLAIM.finditer(message):
            key = ((match.group(1) or match.group(2) or "").lower(), int(match.group(3)))
            found.setdefault(key, []).append(commit)
    return found


def messages(git, *revisions):
    """[(commit, message)] for those commits, or None when their messages could not be read."""
    log = git("log", "--format=%H%x00%B%x01", *revisions)
    if log.returncode: return None
    entries = (entry.strip("\n").partition("\x00") for entry in log.stdout.split("\x01"))
    return [(commit, message) for commit, _, message in entries if commit]


def negated(message):
    """[(line number, that line, issue)] for each closing claim in a commit message that its own clause denies, as
    "This does not fix #43." does: GitHub closes the issue all the same. The clause is read from the text alone, so a
    comma inside a negated clause ends it early, and such a claim passes."""
    found = []
    for match in CLAIM.finditer(message):
        start = match.start()
        # One character past the claim's start, for a line that opens with the keyword's capital.
        ends = list(CLAUSE_END.finditer(message, 0, start + 1))
        clause = message[ends[-1].end() if ends else 0:start]
        denials = list(NEGATION.finditer(clause))
        if denials and len(clause[denials[-1].end():].split()) <= NEGATED_WITHIN:
            line = message.count("\n", 0, start)
            issue = f"{match.group(1) or match.group(2) or ''}#{match.group(3)}"
            found.append((line + 1, message.split("\n")[line], issue))
    return found


def negated_claims(git, *revisions):
    """[(commit, line number, line, issue)] for the negated closing claims in those commits' messages (negated), or
    None when the messages could not be read."""
    read = messages(git, *revisions)
    return None if read is None else [(commit, *claim) for commit, message in read for claim in negated(message)]


def issue_state(repository, number):
    """(OPEN or CLOSED, None) for an issue, or (None, why gh could not tell)."""
    command = ["gh", "issue", "view", str(number), "--json", "state", "--jq", ".state"]
    if repository: command += ["--repo", repository]
    try:
        shown = subprocess.run(command, capture_output=True, text=True, timeout=GH_TIMEOUT, stdin=subprocess.DEVNULL,
                               env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    except FileNotFoundError: return None, "gh is not installed"
    except subprocess.TimeoutExpired: return None, f"gh issue view took over {GH_TIMEOUT}s"
    if shown.returncode: return None, "gh issue view failed: " + (shown.stderr.strip().splitlines() or ["no message"])[0]
    state = shown.stdout.strip()
    return (state, None) if state else (None, "gh issue view printed no state")


def claims(git, base, tips):
    """(report lines, refusals) for a merge into base that brings tips' commits in with no content.
    Each issue those commits claim to close must already be claimed by a commit base holds, so that
    the merge claims nothing new, and must be closed now, or GitHub would close it."""
    brought, held = claimed(git, *tips, f"^{base}"), claimed(git, base)
    if brought is None or held is None: return [], ["the commit messages could not be read"]
    lines, refusals = [], []
    for key, commits in sorted(brought.items()):
        issue = f"{key[0]}#{key[1]}"
        if key not in held:
            refusals.append(f"{commits[0][:12]} claims to close {issue}, and no commit {short(base)} holds does")
            continue
        state, why = issue_state(*key)
        if state == "CLOSED":
            lines.append(f"claim: {issue} by {commits[0][:12]}, and by {held[key][0][:12]} in {short(base)}; closed")
        elif state == "OPEN": refusals.append(f"{commits[0][:12]} claims to close {issue}, which is open, so the merge would close it")
        else: refusals.append(f"{commits[0][:12]} claims to close {issue}, whose state is unknown: {why}")
    return lines, refusals


def main(argv):
    if argv[:1] == ["contained"] and len(argv) == 3:
        how, basis = proof(lambda *arguments: subprocess.run(["git", *arguments], capture_output=True, text=True),
                           argv[1], argv[2])
        if how: print(f"{how} ({basis})" if basis else how)
        return 0
    if argv[:1] == ["citations"]:
        parser = argparse.ArgumentParser(prog="landing.py citations")
        parser.add_argument("tip")
        parser.add_argument("--base", action="append", required=True)
        parser.add_argument("--branch")
        parser.add_argument("--issues", action="store_true")
        arguments = parser.parse_args(argv[1:])
        lines, retain = citations(lambda *words: subprocess.run(["git", *words], capture_output=True, text=True),
                                  arguments.tip, arguments.base, arguments.branch, arguments.issues,
                                  os.environ.get(ISSUES_CACHE))
        for line in lines: print(line)
        if retain: print(f"retain: {retain}")
        return 0
    if argv[:1] == ["claims"] and len(argv) >= 3:
        lines, refusals = claims(lambda *words: subprocess.run(["git", *words], capture_output=True, text=True),
                                 argv[1], argv[2:])
        for line in lines: print(line)
        for refusal in refusals: print(f"refuse: {refusal}")
        return 0
    print("usage: landing.py contained BASE TIP\n"
          "       landing.py citations TIP --base REF [--base REF] [--branch NAME] [--issues]\n"
          "       landing.py claims BASE TIP...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
