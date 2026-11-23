"""Prepare a reviewable patch for a project that carries an exact copy of Queuekeeper's launcher."""
import argparse
from pathlib import Path
import stat
import subprocess
import sys

from . import git_state


LAUNCHER = "scripts/qk"
REGULAR_MODES = {b"100644", b"100755"}
HELP = "print a Git patch to refresh an unchanged, historical stock scripts/qk copy"


def prepare(project, source):
    """Return patch bytes (empty for a current copy), or refuse a copy we cannot identify exactly."""
    environment = {**git_state.environment(), "GIT_NO_LAZY_FETCH": "1"}

    def git(root, *words):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *words], capture_output=True,
                                stdin=subprocess.DEVNULL, env=environment, timeout=git_state.GIT_TIMEOUT)
        if result.returncode:
            raise ValueError(f"could not read Git metadata in {root}: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    if not (source / ".git").exists() or Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source \
            or git(source, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("use a full Queuekeeper source checkout with launcher history")
    if project == source:
        raise ValueError("select a separate project checkout that carries a copy of the launcher")
    if Path(git(project, "rev-parse", "--show-toplevel").decode().strip()).resolve() != project:
        raise ValueError("run from the project checkout root or name it with --project")

    target = project / LAUNCHER
    if target.is_symlink() or target.parent.is_symlink() or not target.is_file():
        raise ValueError(f"{LAUNCHER} must be a regular, tracked file in the project")

    def entry(root, revision):
        metadata = git(root, "ls-tree", revision, "--", LAUNCHER).split(b"\t", 1)[0].split()
        if len(metadata) == 3 and metadata[0] in REGULAR_MODES:
            return metadata[0], metadata[2]
        return None

    committed = entry(project, "HEAD")
    staged = git(project, "ls-files", "--stage", "--", LAUNCHER).split(b"\t", 1)[0].split()
    actual = git(project, "hash-object", "--no-filters", "--", LAUNCHER).strip()
    mode = b"100755" if target.stat().st_mode & stat.S_IXUSR else b"100644"
    if not committed or staged != [*committed, b"0"] or (mode, actual) != committed:
        raise ValueError(f"{LAUNCHER} must be tracked and have no staged or unstaged changes")

    tip = git(source, "rev-parse", "HEAD").decode().strip()
    expected = entry(source, tip)
    if not expected:
        raise ValueError(f"the Queuekeeper source checkout has no regular {LAUNCHER} at HEAD")
    source_format = git(source, "rev-parse", "--show-object-format").strip()
    if source_format != git(project, "rev-parse", "--show-object-format").strip():
        raise ValueError("the project and Queuekeeper use different Git object formats")
    if actual == expected[1]:
        return b""

    history = git(source, "log", "--no-notes", "--no-show-signature", "--format=%H", tip, "--", LAUNCHER)
    for revision in history.decode().splitlines():
        old = entry(source, revision)
        if old and old[1] == actual and old[0] == expected[0]:
            # Include every old line so the reviewed patch has context for the entire launcher.
            return git(source, "diff", "--patch", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--text",
                       "--full-index", "--no-relative", "--src-prefix=a/", "--dst-prefix=b/", "--unified=10000",
                       revision, tip, "--", LAUNCHER)
    raise ValueError(f"{LAUNCHER} does not match a stock copy in this Queuekeeper checkout's launcher history. "
                     "Compare and refresh the launcher by hand")


def main(argv, project, source):
    argparse.ArgumentParser(prog="qk launcher-patch", description=HELP).parse_args(argv)
    try:
        patch = prepare(Path(project or ".").resolve(), source)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 1
    if patch:
        sys.stdout.buffer.write(patch)
    else:
        print(f"queuekeeper: {LAUNCHER} already matches the stock launcher", file=sys.stderr)
    return 0
