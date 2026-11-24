"""Prepare a reviewable patch for a project that carries an exact copy of Queuekeeper's launcher."""
import argparse
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from . import git_state


LAUNCHER = "scripts/qk"
REGULAR_MODES = {b"100644", b"100755"}
HELP = "print a Git patch to refresh an unchanged, historical stock scripts/qk copy"


def git(root, *words, environment=None, patch=None):
    if environment is None:
        environment = {**git_state.environment(), "GIT_NO_LAZY_FETCH": "1"}
    input_options = {"stdin": subprocess.DEVNULL} if patch is None else {"input": patch}
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *words], capture_output=True,
                            env=environment, timeout=git_state.GIT_TIMEOUT, **input_options)
    if result.returncode:
        operation = words[0]
        raise ValueError(f"git {operation} failed in {root}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def prepare(project, source, *, project_revision="HEAD", source_revision="HEAD"):
    """Return patch bytes (empty for a current copy), or refuse a copy we cannot identify exactly."""
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

    committed = entry(project, project_revision)
    staged = git(project, "ls-files", "--stage", "--", LAUNCHER).split(b"\t", 1)[0].split()
    actual = git(project, "hash-object", "--no-filters", "--", LAUNCHER).strip()
    mode = b"100755" if target.stat().st_mode & stat.S_IXUSR else b"100644"
    if not committed or staged != [*committed, b"0"] or (mode, actual) != committed:
        raise ValueError(f"{LAUNCHER} must be tracked and have no staged or unstaged changes")

    tip = git(source, "rev-parse", source_revision).decode().strip()
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


def proposal(project, source, reviewed):
    """Write a one-file commit from reviewed bytes, leaving every checkout index and ref alone."""
    published = "refs/remotes/origin/main"
    base = git(project, "rev-parse", "--verify", f"{published}^{{commit}}").decode().strip()
    tip = git(source, "rev-parse", "--verify", f"{published}^{{commit}}").decode().strip()
    if git(project, "rev-parse", "HEAD").decode().strip() != base:
        raise ValueError("run from a project checkout at its fetched origin/main")
    if git(source, "rev-parse", "HEAD").decode().strip() != tip:
        raise ValueError("use a Queuekeeper source checkout at its fetched origin/main")
    patch = prepare(project, source, project_revision=base, source_revision=tip)
    if not patch or patch != reviewed:
        raise ValueError("the reviewed patch must match the current stock update; regenerate and review it")
    with tempfile.TemporaryDirectory(prefix="qk-launcher-") as directory:
        environment = {**git_state.environment(), "GIT_NO_LAZY_FETCH": "1",
                       "GIT_INDEX_FILE": str(Path(directory) / "index")}
        git(project, "read-tree", base, environment=environment)
        git(project, "apply", "--cached", "--whitespace=nowarn", "-", environment=environment, patch=patch)
        tree = git(project, "write-tree", environment=environment).decode().strip()
    return git(project, "commit-tree", tree, "-p", base, "-m",
               f"Refresh stock Queuekeeper launcher\n\nQueuekeeper source: {tip}").decode().strip()


def main(argv, project, source):
    parser = argparse.ArgumentParser(prog="qk launcher-patch", description=HELP)
    parser.add_argument("--queue", type=Path, metavar="PATCH",
                        help="queue exactly this reviewed patch on the project's normal gate")
    args = parser.parse_args(argv)
    project = Path(project or ".").resolve()
    try:
        if args.queue:
            commit = proposal(project, source, args.queue.read_bytes())
            print(f"queuekeeper: stock launcher proposal {commit}; to retry, run qk land {commit}",
                  file=sys.stderr, flush=True)
            return subprocess.run([sys.executable, str(source / "bin/qk"), "--project", str(project),
                                   "land", commit], cwd=project, env=git_state.environment()).returncode
        patch = prepare(project, source)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"queuekeeper: {error}", file=sys.stderr)
        return 1
    if patch:
        sys.stdout.buffer.write(patch)
    else:
        print(f"queuekeeper: {LAUNCHER} already matches the stock launcher", file=sys.stderr)
    return 0
