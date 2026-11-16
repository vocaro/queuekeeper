"""Whether every test of a project has a lane: qk doctor's reading of what each lane selects (#289).

A test no lane selects goes stale unseen (doc/guides/lanes.md, Every test has a lane). This reads the tests from
the tracked source, without building or running anything, and each lane's selection from its command:

- swift test and qk xctest select their package's test targets, narrowed by --filter and --skip;
- qk unittest and python3 -m unittest discover select the modules their discovery reaches;
- xcodebuild test and qk simulator-tests, with -only-testing:Target/Class, select that class;
- qk spelling, qk docs, qk preflight, qk run (whose lane is read in its own right), swift build, swift run and an xcodebuild
  that tests nothing select no test.

Any other command, a project's own script most often, cannot be read, and queuekeeper.json's testLanes.selects
states what it selects instead. So there are three answers, and could-not-tell is not a pass: a test no readable lane
selects has no lane only when every lane was read; while one could not be, that test may be in it. A test counts as
selected when any lane selects any of it, and which lanes run when is the guide's rule, not this check's.
"""
import argparse
from collections import Counter, namedtuple
import fnmatch
import os
from pathlib import Path
import posixpath
import re
import subprocess

EVERY_TEST_HAS_A_LANE = "every-test-has-a-lane"
TESTS_WITHOUT_A_LANE = "tests-without-a-lane"
COULD_NOT_TELL = "could-not-tell"
NO_TESTS_FOUND = "no-tests-found"
RULE = "doc/guides/lanes.md, Every test has a lane"
# Frozen records at the top of a project, and code a project keeps but did not write, anywhere in it.
RECORDS = ("measurements/", "evidence/")
VENDORED = {"licenses", "ThirdParty", "Vendor"}
# How many names one warning spells out; qk doctor's testLanes holds the rest.
SHOWN = 10

Test = namedtuple("Test", "kind path name")
# filtered: selects by a pattern that may name single tests, so one that matches no class says nothing.
Selection = namedtuple("Selection", "selects filtered")
NOTHING = Selection(lambda test: False, False)

# What hides code from a reading of Swift's declarations, in the order the language sees it.
LITERAL = re.compile(r'""".*?"""|"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)
TOKEN = re.compile(r"@Suite\b|@Test\b|\bfunc\s+(test\w*)|\b(class|struct|enum|actor|extension|protocol)\s+([A-Za-z_][\w.]*)"
                   r"|[{}]")
CLASS = re.compile(r"\bclass\s+(\w+)\s*(?:<[^>{]*>)?\s*:\s*([^{]+)\{")
PYTHON_TEST = re.compile(r"^\s*(?:async\s+)?def\s+test|^\s*class\s+\w+\(.*TestCase.*\)\s*:", re.M)
TEST_TARGET = re.compile(r"\.testTarget\s*\(")


class Unread(Exception):
    """A lane whose selection cannot be read, for the reason given."""


class Words(argparse.ArgumentParser):
    """A parser of a lane's words, which reports them as unread where argparse would exit."""
    def __init__(self):
        super().__init__(add_help=False, allow_abbrev=False)

    def error(self, message):
        raise Unread(f"its arguments do not parse: {message}")


def code_of(source, keep_strings=False):
    """Swift source with comments blanked, and string literals too unless keep_strings."""
    return LITERAL.sub(lambda match: match[0] if keep_strings and match[0].startswith('"') else " ", source)


def read_declarations(code):
    """From one file's code: the types holding a func test..., those holding a Swift Testing test or marked @Suite,
    and whether a @Test stands outside any type."""
    methods, suites, free = set(), set(), False
    stack, pending, suite_next = [], None, False
    def enclosing(): return next((name for name in reversed(stack) if name), None)
    for match in TOKEN.finditer(code):
        token = match[0]
        if token == "{":
            stack.append(pending)
            pending = None
        elif token == "}":
            if stack: stack.pop()
        elif token == "@Suite": suite_next = True
        elif token == "@Test":
            if enclosing(): suites.add(enclosing())
            else: free = True
        elif match[1]:
            if enclosing(): methods.add(enclosing())
        elif match[3] not in ("func", "var", "let"):  # `class func` declares no class
            pending = match[3]
            if suite_next: suites.add(pending)
            suite_next = False
    return methods, suites, free


def swift_tests(sources):
    """The XCTest classes, across the project's files as a class may derive from another file's, and the Swift Testing
    suites, from {path: code}. A class that declares no test of its own is a base, not a test."""
    bases, homes, methods, found = {}, {}, set(), set()
    for path, code in sources.items():
        for name, inherited in CLASS.findall(code):
            bases[name] = {re.split(r"[<\s]", base.strip())[0].split(".")[-1]
                           for base in re.split(r"\bwhere\b", inherited)[0].split(",")}
            homes[name] = path
        held, suites, free = read_declarations(code)
        methods |= held
        if re.search(r"\bimport\s+(?:\w+\s+)?Testing\b", code):
            found |= {Test("swift", path, name) for name in suites}
            if free: found.add(Test("swift", path, posixpath.splitext(posixpath.basename(path))[0]))
    derived, grew = {"XCTestCase"}, True
    while grew:
        more = {name for name, parents in bases.items() if parents & derived} - derived
        derived |= more
        grew = bool(more)
    return found | {Test("swift", homes[name], name) for name in derived & methods & set(homes)}


def tracked_files(root):
    """The checkout's tracked paths: none in a directory that is no repository, which tracks nothing, as qk doctor's
    other checks of a project's history take it. Any other failure of git is the caller's, as nothing was learned."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL)
    if listed.returncode:
        message = listed.stderr.decode(errors="replace").strip()
        if "not a git repository" in message: return []
        raise OSError(message or "git ls-files failed")
    return [os.fsdecode(path) for path in listed.stdout.split(b"\0") if path]


def under(path, directory):
    directory = posixpath.normpath(directory)
    return directory == "." or path.startswith(f"{directory}/")


def listed(path, keys):
    """Whether one of keys, a directory if it ends in / and otherwise a file, holds path."""
    return any(path.startswith(key) if key.endswith("/") else path == key for key in keys)


def is_record(path):
    return path.startswith(RECORDS) or bool(VENDORED & set(path.split("/")[:-1]))


def discover(root, tracked, skip):
    """The project's tests, how many were left out as records, and the files that could not be read."""
    swift, tests, unreadable, skipped = {}, set(), [], set()
    for path in tracked:
        name = posixpath.basename(path)
        python = name.endswith(".py") and (fnmatch.fnmatchcase(name, "test*.py") or name.endswith("_test.py"))
        if not python and not name.endswith(".swift"): continue
        try: text = (root / path).read_text(errors="replace")
        except OSError as error:
            unreadable.append(f"{path}: {error.strerror or error}")
            continue
        if python and not PYTHON_TEST.search(text): continue
        if is_record(path) or listed(path, skip):
            if python or re.search(r"XCTestCase|@Test\b|@Suite\b", code_of(text)): skipped.add(path)
            continue
        if python: tests.add(Test("python", path, posixpath.splitext(path)[0].replace("/", ".")))
        else: swift[path] = code_of(text)
    return sorted(tests | swift_tests(swift)), len(skipped), unreadable


def test_targets(root, package):
    """{target name: its directory, from the root} for the package at root/package, as its manifest spells them."""
    try: manifest = (root / package / "Package.swift").read_text(errors="replace")
    except OSError: raise Unread(f"it tests the package at {package}, which has no readable Package.swift")
    manifest, targets = code_of(manifest, keep_strings=True), {}
    for match in TEST_TARGET.finditer(manifest):
        depth, end = 1, match.end()
        while depth and end < len(manifest):
            depth += {"(": 1, ")": -1}.get(manifest[end], 0)
            end += 1
        call = manifest[match.end():end]
        name, path = re.search(r'\bname:\s*"([^"]+)"', call), re.search(r'\bpath:\s*"([^"]+)"', call)
        if not name: raise Unread(f"a test target in {package}/Package.swift has no literal name")
        targets[name[1]] = posixpath.normpath(posixpath.join(package, path[1] if path else f"Tests/{name[1]}"))
    if not targets: raise Unread(f"{package}/Package.swift spells out no test target this can read")
    return targets


def compiled(patterns):
    try: return [re.compile(pattern) for pattern in patterns]
    except re.error as error: raise Unread(f"its filter is not a regular expression: {error}")


def swift_package(root, words):
    """swift test's or qk xctest's selection."""
    parser = Words()
    parser.add_argument("--package-path", action="append")
    parser.add_argument("--filter", action="append", default=[])
    parser.add_argument("--skip", action="append", default=[])
    known, _ = parser.parse_known_args(words)
    if any("/" in pattern for pattern in known.filter + known.skip):
        raise Unread("its filter or skip names a single test")
    filters, skips = compiled(known.filter), compiled(known.skip)
    targets = {}
    for package in known.package_path or ["."]:
        for target, directory in test_targets(root, posixpath.normpath(package)).items():
            targets[directory] = target
    def selects(test):
        if test.kind != "swift": return False
        for directory, target in targets.items():
            if under(test.path, directory):
                identity = f"{target}.{test.name}"
                return (not filters or any(pattern.search(identity) for pattern in filters)) \
                    and not any(pattern.search(identity) for pattern in skips)
        return False
    return Selection(selects, bool(filters))


def only_testing(words):
    """xcodebuild test's selection, where it spells one out class by class."""
    if any(word.startswith("-skip-testing") for word in words):
        raise Unread("it runs a scheme's tests less those it skips, and this does not read a test plan")
    named = [word[len("-only-testing:"):] for word in words if word.startswith("-only-testing:")]
    if not named or len(named) != sum(word.startswith("-only-testing") for word in words):
        raise Unread("it runs what its scheme's test plan holds, and this does not read a test plan")
    classes = set()
    for name in named:
        parts = name.split("/")
        if len(parts) < 2: raise Unread(f"-only-testing:{name} names a whole target, whose classes this cannot list")
        classes.add(parts[1].removesuffix("()"))
    return Selection(lambda test: test.kind == "swift" and test.name.split(".")[-1] in classes, False)


def unittest_discovery(root, words, discover_form):
    """The modules qk unittest or python3 -m unittest discover reaches."""
    parser = Words()
    parser.add_argument("-s", "--start-directory", default=".")
    parser.add_argument("-p", "--pattern", action="append")
    parser.add_argument("-t", "--top-level-directory")
    parser.add_argument("-k", dest="selectors", action="append", default=[])
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-j", "--jobs")
    parser.add_argument("--split")
    parser.add_argument("--batch-size")
    parser.add_argument("--timeout")
    parser.add_argument("names", nargs="*")
    known = parser.parse_args(words)
    if known.selectors: raise Unread("it selects by -k, a pattern over test names")
    if discover_form and known.names: raise Unread("it gives discover positional arguments")
    start, patterns = posixpath.normpath(known.start_directory), known.pattern or ["test*.py"]
    def selects(test):
        if test.kind != "python" or not under(test.path, start): return False
        relative = posixpath.relpath(test.path, start).split("/")
        # Discovery goes only into packages, so a module under a folder with no __init__.py is out of its reach.
        if not all((root / start / "/".join(relative[:depth]) / "__init__.py").is_file()
                   for depth in range(1, len(relative))): return False
        if not any(fnmatch.fnmatchcase(relative[-1], pattern) for pattern in patterns): return False
        module = ".".join(relative)[:-len(".py")]
        return not known.names or any(name == module or name.startswith(f"{module}.") or module.startswith(f"{name}.")
                                      for name in known.names)
    return Selection(selects, False)


def eng_command(root, words):
    action, rest = words[0], words[1:]
    if action in ("spelling", "docs", "run", "preflight"): return NOTHING
    if action == "xctest": return swift_package(root, rest)
    if action == "simulator-tests": return only_testing(rest)
    if action == "unittest": return unittest_discovery(root, rest, False)
    raise Unread(f"it runs queuekeeper {action}, whose selection this does not read")


def read_command(root, command):
    """A lane command's selection, or Unread."""
    words = list(command)
    if posixpath.basename(words[0]) == "env":
        words = words[1:]
        while words and "=" in words[0] and not words[0].startswith("-"): words = words[1:]
    described = " ".join(words[:3])
    if not words: raise Unread("its env names no command")
    tool = posixpath.basename(words[0])
    if tool == "swift" and words[1:2] == ["test"]: return swift_package(root, words[2:])
    if tool == "swift" and words[1:2] and words[1] in ("build", "run"): return NOTHING
    if tool == "xcodebuild":
        return only_testing(words) if {"test", "test-without-building"} & set(words) else NOTHING
    if re.fullmatch(r"python[\d.]*", tool):
        words = words[1:]
        while words and words[0] in ("-B", "-u", "-E", "-I", "-s", "-S"): words = words[1:]
        if words[:3] == ["-m", "unittest", "discover"]: return unittest_discovery(root, words[3:], True)
        tool = posixpath.basename(words[0]) if words else ""
    if tool == "qk" and len(words) > 1: return eng_command(root, words[1:])
    raise Unread(f"this cannot read which tests `{described}` selects")


def declared(paths):
    """A lane's testLanes.selects: the tests under these paths, a directory ending in /, and none for an empty list."""
    return Selection(lambda test: listed(test.path, paths), False)


def report(root, config, tracked=None):
    """qk doctor's testLanes: the result, one of the four above, and what it rests on. tracked names the project's
    files where git cannot, and otherwise git lists them."""
    root, problems = Path(root).resolve(), []
    settings = config.get("testLanes", {})
    if tracked is None:
        try: tracked = tracked_files(root)
        except (OSError, subprocess.SubprocessError) as error:
            tracked = []
            problems.append(f"the project's files could not be listed: {error}")
    tests, skipped, unreadable = discover(root, tracked, settings.get("skip", {}))
    problems += [f"a test file could not be read: {line}" for line in unreadable]
    lanes = {name: lane for name, lane in config.get("lanes", {}).items() if "command" in lane}
    statements = settings.get("selects", {})
    problems += [f"testLanes.selects names {name}, which is no lane with a command" for name in sorted(set(statements) - set(lanes))]
    selections, unread = [], {}
    for name, lane in sorted(lanes.items()):
        try:
            selection = declared(statements[name]) if name in statements else read_command(root, lane["command"])
            if selection.filtered and not any(selection.selects(test) for test in tests):
                raise Unread("its filter matches no test class, so it may name single tests")
            selections.append(selection)
        except Unread as reason:
            unread[name] = str(reason)
    unselected = [test for test in tests if not any(selection.selects(test) for selection in selections)]
    # While a lane is unread, a test no other lane selects may be in it: not known to have a lane, nor to lack one.
    undetermined, missing = (unselected, []) if unread else ([], unselected)
    if problems or undetermined: result = COULD_NOT_TELL
    elif missing: result = TESTS_WITHOUT_A_LANE
    elif tests: result = EVERY_TEST_HAS_A_LANE
    else: result = NO_TESTS_FOUND
    return {"result": result, "tests": len(tests), "withLane": len(tests) - len(unselected), "skipped": skipped,
            "noLane": [f"{test.path}: {test.name}" for test in missing],
            "undetermined": dict(sorted(Counter(posixpath.dirname(test.path) for test in undetermined).items())),
            "unreadLanes": unread, "problems": problems}


def named(items):
    items = list(items)
    return ", ".join(items[:SHOWN]) + (f" and {len(items) - SHOWN} more" if len(items) > SHOWN else "")


def doctor_warnings(result):
    """qk doctor's words for a result that is not a pass. They are warnings: nothing here stops a gate."""
    if result["result"] == TESTS_WITHOUT_A_LANE:
        return [f"{len(result['noLane'])} test(s) belong to no lane, so nothing runs them and they go stale unseen: "
                f"{named(result['noLane'])}; give each a lane or delete it, and name a record kept on purpose in "
                f"testLanes.skip ({RULE})"]
    if result["result"] != COULD_NOT_TELL: return []
    parts = []
    if result["undetermined"]:
        count = sum(result["undetermined"].values())
        parts.append(f"{count} test(s), in {named(result['undetermined'])}, are selected by no lane this can read, and "
                     f"{len(result['unreadLanes'])} lane(s) cannot be read ({named(result['unreadLanes'])}): state what "
                     "each selects in testLanes.selects, an empty list for one that runs no test")
    parts += result["problems"]
    return [f"could not tell whether every test has a lane, which is not a pass: {'; '.join(parts)} ({RULE})"]
