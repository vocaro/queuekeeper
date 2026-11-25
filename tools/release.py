#!/usr/bin/env python3
"""Cut a Queuekeeper release in two steps (vocaro/queuekeeper#348; the developer guide's "Cutting a release").

    python3 tools/release.py check                 list every version reference; exit 1 when any disagree
    python3 tools/release.py bump X.Y.Z            rewrite every reference to X.Y.Z and commit it on release-X.Y.Z
    python3 tools/release.py publish X.Y.Z         draft the notes and print the tag and release commands
    python3 tools/release.py publish X.Y.Z --yes   ... and run them

`bump` refuses a tree with any change or untracked file, a HEAD other than origin/main once origin is fetched, and a
version no greater than the current one. It commits on a new branch and prints the `qk land` command that lands it;
it does not land by itself. `publish` runs only after that landing: it refuses unless every reference at origin/main
reads X.Y.Z and tag vX.Y.Z does not exist. It drafts the release notes, the install section of v1.0.0's notes with
the new tag and the subjects of the commits since the previous tag, to --notes (by default a file in the temporary
folder), and keeps a notes file that is already there, so that an edit made after a dry run is what --yes publishes.
Without --yes it runs nothing and prints the commands it would run.

The references are the core's pyproject.toml `version`, `queuekeeper.__version__`, the MCP server's `VERSION`, and
each plugins/*/pyproject.toml `version` and exact pin on a queuekeeper package. tests/core/test_release.py fails when
they disagree, or when a plugin or a version constant appears that this script would not rewrite.
"""
import argparse
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CORE = "pyproject.toml"
CONSTANTS = {"src/python/queuekeeper/__init__.py": "__version__",
             "plugins/mcp/src/queuekeeper_mcp/server.py": "VERSION"}
# Python sources searched for a version constant that CONSTANTS does not name.
SOURCES = ("src/python", "plugins")
CONSTANT = re.compile(r"""^(__version__|VERSION)\s*=\s*["']""", re.M)
PIN = re.compile(r'"(queuekeeper[A-Za-z0-9_.-]*)\s*([=<>!~]=?=?)\s*([^";\s]*)')
VERSION = re.compile(r"\d+\.\d+\.\d+")
TEMPLATE = "v1.0.0"  # The release whose notes hold the install section every release repeats.
ISSUE = 348
LAND = "python3 scripts/qk land --wait --json"


class Refused(Exception):
    """A reason the step cannot go on, said as a sentence."""


class Reference:
    """One place a version is written: its file, line, what it is, the version it reads and the span holding it."""

    def __init__(self, path, line, label, value, start, end):
        self.path, self.line, self.label, self.value, self.start, self.end = path, line, label, value, start, end

    def __repr__(self):
        return f"{self.path}:{self.line}: {self.label}{'' if self.label.endswith('==') else ' = '}{self.value}"


class WorkingTree:
    """The files of a checkout as they are on disk."""

    def __init__(self, root):
        self.root = Path(root)

    def read(self, path):
        file = self.root / path
        return file.read_text() if file.is_file() else None

    def plugins(self):
        folder = self.root / "plugins"
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")) if folder.is_dir() else []

    def python(self):
        found = []
        for base in SOURCES:
            for path in (self.root / base).rglob("*.py"):
                parts = path.relative_to(self.root).parts
                if not any(part.startswith(".") or part in ("__pycache__", "build", "node_modules") for part in parts):
                    found.append("/".join(parts))
        return sorted(found)


class Revision:
    """The files of a checkout as one commit holds them."""

    def __init__(self, root, revision):
        self.root, self.revision = Path(root), revision
        self.paths = git(root, "ls-tree", "-r", "--name-only", revision).splitlines()

    def read(self, path):
        if path not in self.paths: return None
        return git(self.root, "show", f"{self.revision}:{path}", strip=False)

    def plugins(self):
        return sorted({p.split("/")[1] for p in self.paths if p.startswith("plugins/") and p.count("/") >= 2})

    def python(self):
        return sorted(p for p in self.paths if p.endswith(".py") and p.startswith(tuple(f"{b}/" for b in SOURCES)))


def git(root, *words, strip=True):
    done = subprocess.run(["git", *words], cwd=root, capture_output=True, text=True)
    if done.returncode:
        raise Refused(f"git {' '.join(words)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip() if strip else done.stdout


def parse(version):
    """X.Y.Z as a tuple of numbers, or a refusal."""
    if not VERSION.fullmatch(version or ""):
        raise Refused(f"{version!r} is not a version of the form X.Y.Z")
    return tuple(int(part) for part in version.split("."))


def at(text, offset):
    return text.count("\n", 0, offset) + 1


def pyproject(path, text, problems):
    """The references in one pyproject.toml, checked against what tomllib reads there."""
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError as error:
        problems.append(f"{path} does not parse: {error}")
        return []
    found = []
    version = re.search(r'^version\s*=\s*"([^"]*)"', text, re.M)
    if "version" not in project or version is None:
        problems.append(f"{path} has no project version")
    elif version.group(1) != project["version"]:
        problems.append(f"{path}: the version line does not hold the project version {project['version']}")
    else:
        found.append(Reference(path, at(text, version.start(1)), "version", version.group(1), version.start(1), version.end(1)))
    pins = [dependency for dependency in project.get("dependencies", []) if dependency.startswith("queuekeeper")]
    located = list(PIN.finditer(text))
    for dependency in pins:
        match = next((m for m in located if f"{m.group(1)}{m.group(2)}{m.group(3)}" == dependency.replace(" ", "")), None)
        if match is None:
            problems.append(f"{path}: cannot find the dependency {dependency!r} in the text")
        elif match.group(2) != "==":
            problems.append(f"{path}: {dependency!r} is not an exact == pin")
        else:
            found.append(Reference(path, at(text, match.start(3)), f"{match.group(1)}==", match.group(3),
                                   match.start(3), match.end(3)))
    if path != CORE and not any(pin.startswith("queuekeeper==") for pin in pins):
        problems.append(f"{path} does not pin the core (queuekeeper==)")
    return found


def survey(tree):
    """Every version reference in tree, and each problem that keeps them from being one release: a disagreement, a
    plugin with no version to rewrite, or a version constant this script does not know."""
    problems, found = [], []
    text = tree.read(CORE)
    if text is None: problems.append(f"{CORE} is missing")
    else: found += pyproject(CORE, text, problems)
    for path, name in CONSTANTS.items():
        text = tree.read(path)
        match = text and re.search(rf'^{re.escape(name)}\s*=\s*"([^"]*)"', text, re.M)
        if not match:
            problems.append(f"{path} has no {name} = \"X.Y.Z\"")
            continue
        found.append(Reference(path, at(text, match.start(1)), name, match.group(1), match.start(1), match.end(1)))
    names = {"queuekeeper"}
    for plugin in tree.plugins():
        path = f"plugins/{plugin}/pyproject.toml"
        text = tree.read(path)
        if text is None:
            problems.append(f"plugins/{plugin} has no pyproject.toml, so its version is not covered")
            continue
        found += pyproject(path, text, problems)
        try: names.add(tomllib.loads(text).get("project", {}).get("name", ""))
        except tomllib.TOMLDecodeError: pass
    for reference in found:
        if reference.label.endswith("==") and reference.label[:-2] not in names:
            problems.append(f"{reference.path}:{reference.line}: pins {reference.label[:-2]}, which is no package here")
    for path in tree.python():
        if path in CONSTANTS: continue
        text = tree.read(path) or ""
        for match in CONSTANT.finditer(text):
            problems.append(f"{path}:{at(text, match.start())}: {match.group(1)} is a version constant tools/release.py "
                            "does not rewrite; add it to CONSTANTS")
    core = next((r.value for r in found if r.path == CORE and r.label == "version"), None)
    for reference in found:
        if core is not None and reference.value != core:
            problems.append(f"{reference.path}:{reference.line}: {reference.label} reads {reference.value}, not {core}")
    return found, problems


def current(tree):
    """The one version every reference agrees on, or a refusal naming each problem."""
    found, problems = survey(tree)
    if problems:
        raise Refused("the version references disagree:\n" + "\n".join(f"  {p}" for p in problems))
    return found[0].value, found


def rewrite(root, references, version):
    """Write version over each reference's span, from the end of each file backward."""
    files = {}
    for reference in references: files.setdefault(reference.path, []).append(reference)
    for path, spans in files.items():
        file = Path(root) / path
        text = file.read_text()
        for reference in sorted(spans, key=lambda r: r.start, reverse=True):
            text = text[:reference.start] + version + text[reference.end:]
        file.write_text(text)


def bump(root, version, out=sys.stdout):
    wanted = parse(version)
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise Refused("the tree has changes or untracked files; bump from a clean checkout of origin/main")
    git(root, "fetch", "-q", "origin", "main")
    head, main = git(root, "rev-parse", "HEAD"), git(root, "rev-parse", "origin/main")
    if head != main:
        raise Refused(f"HEAD ({head[:12]}) is not origin/main ({main[:12]}); bump from current main")
    old, references = current(WorkingTree(root))
    if wanted <= parse(old):
        raise Refused(f"{version} is not greater than the current version {old}")
    branch = f"release-{version}"
    if subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"], cwd=root,
                      capture_output=True).returncode == 0:
        raise Refused(f"branch {branch} already exists")
    git(root, "switch", "-q", "-c", branch)
    rewrite(root, references, version)
    now, _ = current(WorkingTree(root))
    assert now == version, now
    git(root, "commit", "-q", "-a", "-m", f"Release Queuekeeper {version}\n\nRefs #{ISSUE}")
    print(f"Rewrote {len(references)} references in {len({r.path for r in references})} files from {old} to "
          f"{version} and committed them on {branch}.", file=out)
    print(f"Land it through the queue, then run `python3 tools/release.py publish {version}`:\n\n  {LAND}", file=out)


def install_section(body, tag):
    """The install section of the template release's notes, naming tag instead."""
    match = re.search(r"^(#+) Install\b", body, re.M)
    if match is None:
        raise Refused(f"{TEMPLATE}'s release notes have no Install section")
    level = len(match.group(1))
    end = re.compile(rf"^#{{1,{level}}} ", re.M).search(body, match.end(1))
    return body[match.start():end.start() if end else len(body)].strip().replace(TEMPLATE, tag)


def notes(root, tag, previous, main):
    done = subprocess.run(["gh", "release", "view", TEMPLATE, "--json", "body", "--jq", ".body"], cwd=root,
                          capture_output=True, text=True)
    if done.returncode:
        raise Refused(f"gh release view {TEMPLATE} failed: {done.stderr.strip()}")
    span = f"{previous}..{main}" if previous else main
    subjects = git(root, "log", "--no-merges", "--format=%s", span).splitlines()
    since = f"since {previous}" if previous else "in this release"
    changes = "\n".join(f"- {subject}" for subject in subjects) or "- (none)"
    return f"{install_section(done.stdout, tag)}\n\n### Changes {since}\n\n{changes}\n"


def publish(root, version, yes=False, notes_path=None, out=sys.stdout):
    parse(version)
    tag = f"v{version}"
    git(root, "fetch", "-q", "--tags", "origin", "main")
    if subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"], cwd=root,
                      capture_output=True).returncode == 0:
        raise Refused(f"tag {tag} already exists")
    main = git(root, "rev-parse", "origin/main")
    found, problems = survey(Revision(root, main))
    wrong = problems + [f"{r!r}, not {version}" for r in found if r.value != version]
    if wrong:
        raise Refused(f"origin/main ({main[:12]}) does not hold the bump to {version}; land "
                      f"`python3 tools/release.py bump {version}` first:\n" + "\n".join(f"  {w}" for w in wrong))
    described = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", main], cwd=root,
                               capture_output=True, text=True)
    previous = described.stdout.strip() if described.returncode == 0 else None
    path = Path(notes_path) if notes_path else Path(tempfile.gettempdir()) / f"queuekeeper-{tag}-notes.md"
    if path.exists():
        print(f"Keeping the release notes already at {path}; delete it to draft them again.", file=out)
    else:
        path.write_text(notes(root, tag, previous, main))
        print(f"Drafted the release notes at {path}.", file=out)
    commands = [["git", "tag", "-a", tag, "-m", f"Queuekeeper {version}", main],
                ["git", "push", "origin", tag],
                ["gh", "release", "create", tag, "--verify-tag", "--title", f"Queuekeeper {version}",
                 "--notes-file", str(path)]]
    print(("Running:" if yes else "Dry run; with --yes these run:"), file=out)
    for command in commands:
        print(f"  {shlex.join(command)}", file=out)
    if not yes: return
    for command in commands:
        out.flush()
        if subprocess.run(command, cwd=root).returncode:
            raise Refused(f"{shlex.join(command)} failed; the steps before it are done")
    print(f"Published {tag} at {main[:12]}.", file=out)


def check(root, out=sys.stdout):
    found, problems = survey(WorkingTree(root))
    for reference in found: print(reference, file=out)
    for problem in problems: print(f"problem: {problem}", file=out)
    if problems: raise Refused(f"{len(problems)} problems")


def main(argv=None, root=ROOT, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="tools/release.py", description=__doc__.split("\n\n")[0])
    steps = parser.add_subparsers(dest="step", required=True)
    steps.add_parser("check", help="list every version reference and whether they agree")
    step = steps.add_parser("bump", help="rewrite every version reference and commit it on release-X.Y.Z")
    step.add_argument("version")
    step = steps.add_parser("publish", help="tag the landed bump and create its GitHub release")
    step.add_argument("version")
    step.add_argument("--yes", action="store_true", help="run the commands instead of printing them")
    step.add_argument("--notes", help="the release notes file to draft or keep")
    arguments = parser.parse_args(argv)
    try:
        if arguments.step == "check": check(root, out)
        elif arguments.step == "bump": bump(root, arguments.version, out)
        else: publish(root, arguments.version, arguments.yes, arguments.notes, out)
    except Refused as refusal:
        print(f"release.py: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
