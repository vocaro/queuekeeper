"""Whether a commit's work is in a base branch, and whether the last ref to it may go: the proofs
that Closeout's landed.sh (skills/closeout/scripts) and `eng disk` share, so that they never
disagree about the same commits.

When main holds a branch's work only as equal patches, a squash or a pull request, the branch is
the last ref to its own commits, and deleting it leaves them to git gc. Evidence names commits by
their IDs, so such a ref stays while main's tree or the project's open issues cite one of its
commits, while an open issue names the branch, or while either could not be read.

landed.sh adds what needs the network: a merged pull request, and the open issues, read with gh.
`eng disk` never fetches and reads neither. landed.sh runs this file as a script:

    python3 -I landing.py contained BASE TIP
    python3 -I landing.py citations TIP --base REF [--base REF] [--branch NAME] [--issues]

It imports nothing from the toolkit, so it runs isolated from the checkout it examines, and it
keeps to Python 3.9, the second Mac's /usr/bin/python3.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ANCESTOR, PATCH_EQUIVALENT, CONTENT_MERGED = "ancestor", "patch-equivalent", "content-merged"
# A commit is cited by a prefix of its ID: 7 to 40 hexadecimal digits that stand as a word.
CITATION = r"[0-9a-fA-F]{7,40}"
WORD = re.compile(rf"(?<![0-9A-Za-z_]){CITATION}(?![0-9A-Za-z_])")
# Open issues one lookup reads; a project with as many has more, so its lookup is incomplete.
ISSUE_LIMIT = 1000
GH_TIMEOUT = 60
# A file in which the sweep's first lookup leaves the open issues, or why they could not be read,
# for every later one in the same sweep.
ISSUES_CACHE = "CLOSEOUT_ISSUES_CACHE"


def contained(git, base, tip):
    """How commit tip is in base, or None when nothing here proves it:

    - ancestor: tip is reachable from base (a merge or a fast-forward).
    - patch-equivalent: every commit has one with the same diff in base (a rebase or a
      cherry-pick), and none is a merge, whose own change a patch comparison never sees.
    - content-merged: merging tip into base would change nothing (a squash). A squash that base
      has since edited over proves none of these.

    git(*arguments) runs git in the repository and returns its CompletedProcess."""
    if git("merge-base", "--is-ancestor", tip, base).returncode == 0: return ANCESTOR
    merges = git("rev-list", "--count", "--merges", f"{base}..{tip}")
    cherry = git("cherry", base, tip)
    if merges.returncode or cherry.returncode: return None
    if merges.stdout.strip() == "0" and not any(line.startswith("+") for line in cherry.stdout.splitlines()):
        return PATCH_EQUIVALENT
    merged = git("merge-tree", "--write-tree", base, tip)
    tree = git("rev-parse", f"{base}^{{tree}}")
    if merged.returncode == 0 and tree.returncode == 0 and merged.stdout.split("\n", 1)[0] == tree.stdout.strip():
        return CONTENT_MERGED
    return None


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


def main(argv):
    if argv[:1] == ["contained"] and len(argv) == 3:
        how = contained(lambda *arguments: subprocess.run(["git", *arguments], capture_output=True, text=True),
                        argv[1], argv[2])
        if how: print(how)
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
    print("usage: landing.py contained BASE TIP\n"
          "       landing.py citations TIP --base REF [--base REF] [--branch NAME] [--issues]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
