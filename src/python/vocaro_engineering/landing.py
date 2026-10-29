"""Whether a commit's work is in a base branch: the one containment proof that Closeout's landed.sh
(skills/closeout/scripts) and `eng disk` share, so that they never disagree about the same commits.

landed.sh adds a proof that needs the network, a merged pull request; `eng disk` never fetches and
uses none. landed.sh runs this file as a script, which prints the method or nothing:

    python3 -I landing.py contained BASE TIP

It imports nothing from the toolkit, so it runs isolated from the checkout it examines, and it
keeps to Python 3.9, the second Mac's /usr/bin/python3.
"""
import subprocess
import sys

ANCESTOR, PATCH_EQUIVALENT, CONTENT_MERGED = "ancestor", "patch-equivalent", "content-merged"


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


def main(argv):
    if len(argv) != 3 or argv[0] != "contained":
        print("usage: landing.py contained BASE TIP", file=sys.stderr)
        return 2
    how = contained(lambda *arguments: subprocess.run(["git", *arguments], capture_output=True, text=True),
                    argv[1], argv[2])
    if how: print(how)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
