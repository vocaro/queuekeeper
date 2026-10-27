"""Shared engineering entry point. Initialization never starts a build or hosted job."""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import __version__, devices, disk, docs, evidence, parallel, processes, release, remote, resources, spelling
from .config import TOOLKIT_ROOT, CAPABILITIES, DEFAULT_TOOLKIT, load, source_identity
from .resources import BUSY, FREE_WAIT
from .simulators import matrix, pool as simulator_pool


def project_adapter(toolkit):
    """The toolkit's project adapter, integrations/shared.py, which init installs as
    scripts/_engineering.py and the launcher imports."""
    spec = importlib.util.spec_from_file_location("_engineering", toolkit / "integrations/shared.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter


def named_checkout(toolkit):
    """The toolkit checkout a new project names: the main checkout when toolkit is a linked
    worktree, such as a session's, which is removed with its session. The project adapter's
    main_checkout finds it, as the launcher does."""
    main = project_adapter(toolkit).main_checkout(toolkit)
    return main if main and (main / "src/python/vocaro_engineering").is_dir() else toolkit


# Where pinned model weights and other verified downloads live, outside every checkout and outside
# /private/tmp, which a reboot empties (guides/shared-host.md, Scratch and result directories).
MODEL_STORAGE = "~/Development/models"
# A path under /tmp or /private/tmp, and the variable it is assigned to when that variable names a model.
TEMPORARY_PATH = re.compile(r"(\b[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*=)?(?<![\w.~/-])((?:/private)?/tmp/[^\s`'\"<>()\[\]{},;|]+)")
MODEL_WORDS = re.compile(r"(?<![a-z])(?:models|weights)(?![a-z])", re.IGNORECASE)


def temporary_model_storage(root, config):
    """A warning when the project's tracked Markdown keeps model storage under /tmp or /private/tmp:
    a path there that names models or weights, or that a MODEL variable is set to. A reboot empties
    /private/tmp, and every model kept there with it. Records under a measurements or evidence
    folder keep the paths their runs used, so they are not read."""
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *(f"*{s}" for s in sorted(docs.MARKDOWN))],
                                capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    places = []
    for relative in sorted(filter(None, listed.split("\0"))):
        if disk.EVIDENCE_NAMES & set(Path(relative).parts[:-1]): continue
        try: text = (root / relative).read_text(errors="replace")
        except OSError: continue
        paths, lines = {}, []
        for number, line in enumerate(text.splitlines(), 1):
            for match in TEMPORARY_PATH.finditer(line):
                path = match.group(2).rstrip(".:")
                if match.group(1) or MODEL_WORDS.search(path):
                    paths[path] = None
                    if number not in lines: lines.append(number)
        if paths:
            places.append(f"{relative} (line{'s' if len(lines) > 1 else ''} {', '.join(map(str, lines))}: "
                          f"{', '.join(paths)})")
    if not places: return []
    return [f"{'; '.join(places)} name{'s' if len(places) == 1 else ''} model storage under /tmp, which a reboot "
            "empties: every model there is gone after a restart and must be downloaded and verified again. Keep "
            f"pinned models and other verified downloads in {MODEL_STORAGE}/{config['name']}/ "
            "(guides/shared-host.md, Scratch and result directories)"]


# The Swift manifests that can name a toolkit package by a path relative to themselves.
MANIFESTS = ("Package.swift", "project.yml", "project.pbxproj")


def unlinked_packages(root, config):
    """Warnings for toolkit packages that the project's tracked Swift manifests name by a relative
    path that does not resolve from this checkout, as from a linked worktree with no toolkit beside
    it. The launcher falls back to the main checkout's sibling; Xcode and SwiftPM do not."""
    name = re.escape(Path(config.get("toolkit", {}).get("path", DEFAULT_TOOLKIT)).name)
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *(f"*{m}" for m in MANIFESTS)],
                                capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    links = {}
    for relative in filter(None, listed.split("\0")):
        manifest = root / relative
        if manifest.name not in MANIFESTS: continue
        # An Xcode project's paths are relative to the folder that holds the .xcodeproj.
        base = manifest.parent.parent if manifest.name == "project.pbxproj" else manifest.parent
        try: text = manifest.read_text(errors="replace")
        except OSError: continue
        for match in re.finditer(rf"((?:\.\./)+{name})/src/swift/[\w.-]+", text):
            if not (base / match.group(0)).exists():
                links.setdefault(Path(os.path.normpath(base / match.group(1))), set()).add(relative)
    return [f"{', '.join(sorted(manifests))} name{'s' if len(manifests) == 1 else ''} toolkit packages under {link}, "
            "which does not exist from this checkout: link the toolkit there with "
            f"`ln -s {named_checkout(TOOLKIT_ROOT)} {link}` (guides/adoption.md)"
            for link, manifests in sorted(links.items())]


def initialize(root, args):
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "engineering.json"
    if config_path.exists(): load(root)
    if not config_path.exists():
        name = args.name or root.name
        # A pool's members are named after it, as `NAME iPhone 17 Pro 1`.
        pool = {"simulatorPool": name} if "simulator-pool" in args.capability else {}
        # The launcher tries toolkit.path beside the checkout it runs in, then beside the main
        # checkout, so a path relative to the main checkout resolves from every worktree of it.
        base = project_adapter(TOOLKIT_ROOT).main_checkout(root) or root
        config_path.write_text(json.dumps({"schemaVersion": 1, "name": name,
            "toolkit": {"path": os.path.relpath(named_checkout(TOOLKIT_ROOT), base)}, "integration": "direct-main",
            "capabilities": args.capability, **pool, "lanes": {}, "exceptions": []}, indent=2) + "\n")
    ignored = root / ".gitignore"
    if not ignored.exists():
        ignored.write_text(".build/\n__pycache__/\n*.pyc\n")
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    launcher = scripts / "eng"
    if not launcher.exists():
        shutil.copyfile(TOOLKIT_ROOT / "integrations/eng.py", launcher)
        launcher.chmod(0o755)
    # The launcher finds the toolkit through this adapter, also from a linked worktree.
    adapter = scripts / "_engineering.py"
    if not adapter.exists():
        shutil.copyfile(TOOLKIT_ROOT / "integrations/shared.py", adapter)
    agents = root / "AGENTS.md"
    if not agents.exists():
        agents.write_text("# Contributor instructions\n\n## Shared engineering guidance\n\n"
            "Before work, run `python3 scripts/eng instructions` to read the common rules and enabled\n"
            "capabilities, then follow the project-specific instructions below. `engineering.json` locates\n"
            "the shared `vocaro-engineering` checkout; set `VOCARO_ENGINEERING_ROOT` for another location.\n"
            "Before shared device, foreground or runner use, run `python3 scripts/eng policy refresh` and\n"
            "read this project's current main instructions.\n\n"
            "Record project-specific product rules and justified exceptions here.\n")
    # Claude Code loads an imported AGENTS.md at session start; it may not follow a link.
    bridge = root / "CLAUDE.md"
    if not bridge.exists(): bridge.write_text("@AGENTS.md\n")
    load(root)
    print(f"Initialized {root}; existing instructions and commands were preserved")


def behind_main(root):
    """How many commits of origin/main, as last fetched, the checkout at root lacks. It never fetches."""
    try:
        count = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD..refs/remotes/origin/main"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    return int(count.stdout) if count.returncode == 0 else 0


def instructions(config):
    behind = behind_main(TOOLKIT_ROOT)
    if behind:
        print(f"Note: the toolkit checkout {TOOLKIT_ROOT} lacks {behind} commit(s) of origin/main as last fetched, "
              f"so these rules may be out of date. Print the current ones with `git -C {TOOLKIT_ROOT} show "
              "origin/main:agents/core.md`, or update the checkout (guides/adoption.md, Updating and rolling back).\n")
    print((TOOLKIT_ROOT / "agents/core.md").read_text())
    for capability in config.get("capabilities", []):
        print((TOOLKIT_ROOT / "agents/capabilities" / f"{capability}.md").read_text())
    for exception in config.get("exceptions", []):
        print(f"Project exception — {exception['rule']}: {exception['reason']}")


def refresh_policy(root, stream=None):
    subprocess.run(["git", "-C", str(TOOLKIT_ROOT), "fetch", "origin",
                    "refs/heads/main:refs/remotes/origin/main"], check=True, timeout=120)
    commit = subprocess.check_output(["git", "-C", str(TOOLKIT_ROOT), "rev-parse", "origin/main"], text=True).strip()
    text = subprocess.check_output(["git", "-C", str(TOOLKIT_ROOT), "show", f"{commit}:policies/shared-host.md"], text=True)
    print(f"Policy source: {commit}\n\n{text}", file=stream)
    return commit


def record(directory, receipt, work):
    """Run work, then write its exit status or failure beside the source identities."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        result = work()
        receipt["exitStatus"] = result
    except BaseException as error:
        # A timeout is a wall-clock backstop that a busy host can reach, not a verdict on the work.
        receipt["outcome"] = "unmeasured" if isinstance(error, processes.TimedOut) else "failed-or-interrupted"
        receipt["error"] = str(error)
        raise
    finally:
        (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return result


def conclude(directory, receipt, status, **fields):
    """Record a completed run's verdict, the status eng run returns, beside its command's exit
    status: passed only for 0, so a zero exit that ran too few tests is failed."""
    receipt.update(outcome="passed" if status == 0 else "unmeasured" if status == evidence.UNMEASURED else "failed",
                   **fields)
    (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")


def judge(lane, result, log, extra=(), expect=None):
    """A lane's status and unmeasured tests from its command's status and its log. A completed run
    that executed too few tests raises ValueError."""
    measured = result in (0, evidence.UNMEASURED)
    if measured and (lane.get("minimumTests") or expect):
        # Arguments such as a filter select part of the lane, which must still run a test.
        minimum = 1 if extra or expect else lane["minimumTests"]
        try:
            evidence.require_tests(log, minimum, expected=expect)
        except ValueError as error:
            # An unmeasured part may not have run at all, so the run stays unmeasured. A test the
            # host left unmeasured ran nothing, so it can also be what leaves a run short.
            if result == 0 and not evidence.unmeasured_tests(log): raise
            print(f"eng: {error}", file=sys.stderr)
    unmeasured = evidence.unmeasured_tests(log) if measured else []
    if unmeasured or result == evidence.UNMEASURED:
        # A skip or cancellation leaves the command's own status at 0, and unmeasured is never green.
        if unmeasured:
            print(f"eng: unmeasured: {len(unmeasured)} test(s) reported unmeasured:", *unmeasured,
                  sep="\n", file=sys.stderr)
        return evidence.UNMEASURED, unmeasured
    return result, []


def admit(root, args):
    # A command always runs here, in the current directory: its paths, devices and products are this
    # Mac's (guides/shared-host.md, Which Mac runs a lane). eng remote admit sends one on purpose.
    # Without a leading --, argparse collected the command, and a later -- belongs to it.
    command = args.command + ([] if args.passed is None else ["--", *args.passed]) if args.command else args.passed
    if not command: raise ValueError("Name the command: eng admit [--exclusive | --model] -- COMMAND")
    # The policy goes to stderr so that stdout carries only the command's own output.
    receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
               "policy": refresh_policy(root, sys.stderr), "command": command,
               "directory": str(Path.cwd()), "exclusive": args.exclusive, "model": args.model}
    with processes.forwarding_signals(), \
            resources.Admission(" ".join(command), args.exclusive, args.timeout, args.model) as admission:
        return record(root / ".build/engineering/admit", receipt,
                      lambda: processes.supervise(command, cwd=Path.cwd(), on_stop=admission.interrupted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init")
    init.add_argument("--name")
    init.add_argument("--capability", action="append", choices=sorted(CAPABILITIES), default=[])
    commands.add_parser("doctor")
    commands.add_parser("instructions")
    policy = commands.add_parser("policy")
    policy.add_argument("operation", choices=["refresh"])
    run = commands.add_parser("run", usage="%(prog)s [-h] [--expect N] lane [-- ARGUMENTS ...]",
        help="run a configured lane, admitting a heavy or exclusive one: eng run "
        "LANE [-- ARGUMENTS], which appends the arguments, such as a test filter, to the lane's command")
    run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests; without it, a "
        "run with arguments needs at least one in place of the lane's minimumTests")
    where = run.add_mutually_exclusive_group()
    where.add_argument("--here", action="store_true", help="run a buildHost lane on this Mac rather than on the "
        f"build host first; when the build host is misconfigured (exit {remote.MISCONFIGURED}), only with the owner's "
        "decision")
    where.add_argument("--build-host-only", action="store_true", help="offer a buildHost lane to the build host, "
        f"which runs it only if it can start it now, and never run it here: exit {BUSY} when the build host declines "
        f"it, {remote.MISCONFIGURED} when it is misconfigured, else the lane's status there; for a caller that holds "
        "or will take its own admission here")
    run.add_argument("--if-free", action="store_true", help=f"run only if heavy-work admission admits the lane within "
        f"{FREE_WAIT}s; otherwise exit {BUSY} without running it")
    run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on this "
        "Mac: the reference a failure seen only side by side is checked against")
    run.add_argument("lane")
    # Listed for help only: main passes everything after these to their own parsers.
    commands.add_parser("simulator-tests", help="build a scheme's tests once and run them on several members of the "
        "project's simulator pool side by side: eng simulator-tests --scheme S --runtime R --device T --device T; "
        "-h for the rest")
    commands.add_parser("simulator-pool", help="list, take, release or prune members of the project's simulator "
        "pool under device-record reservations: eng simulator-pool list|take|release|prune; -h for the rest")
    commands.add_parser("unittest", help="run a unittest suite's modules in processes of their own, side by side: "
        "eng unittest [-s START] [-p PATTERN] [-j JOBS] [-v]; eng unittest -h for the rest")
    admission =commands.add_parser("admit", help="run any other heavy command (a build, swift test, xcodebuild "
        "test, a simulator run, a gate suite) through the host's heavy-work admission: eng admit -- COMMAND; add "
        "--model for a generative-model run, and --exclusive only for work that needs the host to itself")
    kind = admission.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot, and the model with them: a "
                      "whole-machine or timing measurement, or a model run that is timed or compared with a baseline")
    kind.add_argument("--model", action="store_true", help="hold one slot and the host's model lock, which one "
                      "model run holds at a time: on-device generation, on this Mac or in a simulator, another local "
                      "generative model on the GPU, or Core ML whose compute units include the GPU")
    admission.add_argument("--timeout", type=float, default=5400, help="seconds to wait for admission (default 5400)")
    admission.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --; "
        "it runs in the current directory")
    logged = commands.add_parser("admissions", help="summarize this host's heavy-work admission log: for each kind "
        "and label, the requests admitted and withdrawn, how long they waited and held, and how admissions ended")
    logged.add_argument("--since", type=resources.local_time, metavar="WHEN", help="count requests made from WHEN, "
                        "an ISO 8601 date or time, local unless it names a zone")
    logged.add_argument("--until", type=resources.local_time, metavar="WHEN", help="count requests made before WHEN")
    logged.add_argument("logs", nargs="*", type=Path, metavar="log", help="a copy of a log, such as the build host's, "
                        "to read instead of this host's; name the previous generation first")
    distant = commands.add_parser("remote", help="run a lane or other heavy command on the build host, the owner's "
        "second Mac, over SSH: eng remote run LANE [-- ARGUMENTS] or eng remote admit -- COMMAND; eng remote passed "
        "LANE says whether the whole lane passed there at a commit")
    operations = distant.add_subparsers(dest="operation", required=True)
    remote_run = operations.add_parser("run", usage="%(prog)s [-h] [--host HOST] [--expect N] lane [-- ARGUMENTS ...]",
        help="run a configured lane on the build host at HEAD, or at a snapshot commit of uncommitted work")
    remote_admit = operations.add_parser("admit", help="run a heavy command on the build host, in its checkout's "
        "root, at HEAD or at a snapshot commit of uncommitted work")
    remote_setup = operations.add_parser("setup", help="install on the build host, with Homebrew, the tools "
        f"every project's gates use ({', '.join(remote.SETUP_FORMULAE)}) and the formulae named, then check them as "
        "eng remote sees the build host; Homebrew itself the owner installs")
    remote_setup.add_argument("formulae", nargs="*", metavar="formula", help="a further Homebrew formula to install")
    remote_setup.add_argument("--import", dest="imports", action="append", metavar="MODULE",
                              help="check that the build host's python3 imports MODULE (repeatable)")
    remote_setup.add_argument("--check", action="store_true", help="only check; install or copy nothing")
    remote_setup.add_argument("--runtimes", action="store_true", help="instead, give this Mac and the build host the "
        "union of the simulator runtimes that the Xcode at this Mac's developer directory lists, copying each one's "
        "disk image over SSH to the Mac that lacks it; it deletes none, and exits 0 only when both list the same")
    for choice in (remote_run, remote_admit, remote_setup):
        choice.add_argument("--host", help="the build host's SSH destination (default: VOCARO_BUILD_HOST, else "
                             f"the ~/.ssh/config alias {remote.DEFAULT_HOST})")
    remote_run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests")
    remote_run.add_argument("lane")
    kind = remote_admit.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot of the build host")
    kind.add_argument("--model", action="store_true", help="hold one slot and the model lock of the build host")
    remote_admit.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --")
    remote_passed = operations.add_parser("passed", help="exit 0 when the whole lane passed on the build host at "
        "exactly this commit; a snapshot's run never counts")
    remote_passed.add_argument("lane")
    remote_passed.add_argument("--commit", default="HEAD")
    publication = commands.add_parser("release", help="tag a library's release and publish it on GitHub once its "
        "gate passed at the commit and origin/main holds it: eng release X.Y.Z COMMIT --notes NOTES.md; each step "
        "stops the rest when it fails, and a run that stopped part way can run again (guides/releasing.md)")
    publication.add_argument("version", metavar="X.Y.Z")
    publication.add_argument("commit", metavar="COMMIT")
    publication.add_argument("--notes", required=True, type=Path, help="the release notes, in a file in the checkout "
                             "that Git does not ignore, which eng spelling must pass")
    evid = commands.add_parser("evidence")
    evid.add_argument("operation", choices=["verify"])
    evid.add_argument("directory", type=Path)
    documentation = commands.add_parser("docs")
    documentation.add_argument("operation", choices=["check"])
    documentation.add_argument("paths", nargs="*")
    spell = commands.add_parser("spelling", help="find British spellings in the project's text, identifiers "
        "included, and file names; exit 1 on any, 2 when nothing was checked")
    spell.add_argument("paths", nargs="*", help="files or directories; untracked files under them are read too")
    knowledge = commands.add_parser("knowledge")
    knowledge.add_argument("query", nargs="?", default="")
    source = commands.add_parser("source")
    source.add_argument("name", choices=["voiceover-ax-observer.swift", "voiceover-link-verbosity.swift",
                                        "probe-simulator-voiceover-activation.m"])
    disk.add_arguments(commands.add_parser("disk", help="inventory reclaimable disk space; changes nothing without --delete"))
    # Everything after the first -- belongs to the command. Python 3.9's argparse rejects options
    # before it and drops a later --, so argparse never sees it.
    argv = sys.argv[1:] if argv is None else list(argv)
    # unittest's options are its own runner's, which this parser would refuse. It reads no project.
    first = 2 if argv[:1] == ["--project"] else 1 if argv[:1] and argv[0].startswith("--project=") else 0
    if argv[first:first + 1] == ["unittest"]: return parallel.unittest_main(argv[first + 1:])
    project = argv[1] if first == 2 else argv[0].split("=", 1)[1] if first == 1 else None
    if argv[first:first + 1] == ["simulator-tests"]: return matrix.main(argv[first + 1:], project)
    if argv[first:first + 1] == ["simulator-pool"]: return simulator_pool.main(argv[first + 1:], project)
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    args.passed = argv[split + 1:] if split < len(argv) else None
    if args.passed is not None and (args.action, getattr(args, "operation", None)) not in {
            ("run", None), ("admit", None), ("remote", "run"), ("remote", "admit")}:
        parser.error("only run and admit take arguments after --")
    root = args.project.resolve()
    try:
        if args.action == "init":
            initialize(root, args)
            return 0
        # The inventory and the admission log cover the whole host, so they need no project configuration.
        if args.action == "disk": return disk.execute(args)
        if args.action == "admissions": return resources.report(args.logs or None, args.since, args.until)
        config = load(root)
        if args.action == "instructions": instructions(config)
        elif args.action == "doctor":
            required = {"git", "python3"}
            if any(c.startswith("simulator") or c == "packaging" for c in config.get("capabilities", [])):
                required |= {"xcrun", "xcodebuild"}
            missing = sorted(name for name in required if not shutil.which(name))
            warnings = []
            heavy = sorted({"simulator-pool", "measurement", "voiceover"}
                           & set(config.get("capabilities", [])))
            if heavy and not any(lane.get("resource") in {"heavy", "exclusive"} for lane in config.get("lanes", {}).values()):
                warnings.append(f"{', '.join(heavy)} work is heavy, but no lane is heavy or exclusive: add heavy lanes "
                                "for the tests and the gate (guides/adoption.md)")
            if "integration" not in config:
                warnings.append("engineering.json declares no integration: add \"integration\": \"direct-main\", "
                                "or \"pull-request\" for a project that takes changes only by pull request (agents/core.md)")
            warnings += temporary_model_storage(root, config)
            warnings += unlinked_packages(root, config)
            # A difference between the two Macs, or a build host that cannot be reached, is an error.
            hosts = remote.compare_hosts()
            print(json.dumps({"project": config["name"], "toolkit": str(TOOLKIT_ROOT),
                "source": source_identity(TOOLKIT_ROOT), "capabilities": config.get("capabilities", []),
                "missingCommands": missing, "warnings": warnings, "buildHost": hosts}, indent=2))
            return int(bool(missing or hosts["errors"]))
        elif args.action == "policy": refresh_policy(root)
        elif args.action == "admit": return admit(root, args)
        elif args.action == "remote": return remote.main(root, config, args)
        elif args.action == "release": return release.main(root, config, args)
        elif args.action == "source":
            from .support import source_path
            print(source_path(args.name))
        elif args.action == "docs":
            failures = docs.check(root, args.paths or None)
            print("\n".join(failures) if failures else "Documentation checks passed")
            return int(bool(failures))
        elif args.action == "spelling": return spelling.check(root, config, args.paths)
        elif args.action == "evidence": print(f"Verified {evidence.verify(args.directory)} evidence files")
        elif args.action == "knowledge":
            entries = json.loads((TOOLKIT_ROOT / "knowledge/index.json").read_text())["entries"]
            for item in entries:
                if args.query.lower() in (item["title"] + " " + item["path"]).lower():
                    print(f"{item['id']}: {item['title']} — {TOOLKIT_ROOT / item['path']}")
        elif args.action == "run":
            lane = config.get("lanes", {}).get(args.lane)
            if lane is None: raise ValueError(f"No configured lane: {args.lane}")
            if args.expect is not None and args.expect < 1: raise ValueError("--expect needs at least 1 test")
            # A foreground lane takes the owner's screen: only the session holding mac-foreground runs it.
            if lane.get("foreground"): devices.require_foreground(os.environ.get(devices.OWNER))
            extra = args.passed or []
            group = lane.get("parallel")
            if group is not None and (extra or args.expect):
                raise ValueError(f"{args.lane} runs a group of lanes, which takes no arguments or --expect; "
                                 "pass them to one of its lanes")
            if group is None and args.serial: raise ValueError(f"--serial runs a lane group, and {args.lane} is not one")
            if args.build_host_only:
                if not lane.get("buildHost"):
                    raise ValueError(f"{args.lane} is not marked buildHost, so --build-host-only cannot offer it to "
                                     "the build host")
                if args.serial: raise ValueError("--serial runs a lane group here, and --build-host-only never does")
                # The caller runs the lane under its own admission when the build host declines it.
                return remote.first(root, config, args.lane, extra, args.expect, fallback=False)
            # Nested work runs inside the admission it inherits, here. The serial reference runs here too.
            if lane.get("buildHost") and not args.here and not args.serial and not os.environ.get(resources.TOKEN):
                status = remote.first(root, config, args.lane, extra, args.expect)
                if status is not None: return status
            resource = lane.get("resource", "light")
            policy_commit = refresh_policy(root) if resource != "light" else None
            directory = root / ".build/engineering" / args.lane
            receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
                       "policy": policy_commit}
            if group is None:
                command = [*lane["command"], *extra]
                receipt.update(command=command, arguments=extra)
                def execute(on_stop=None):
                    return record(directory, receipt, lambda: processes.run(command, cwd=root,
                                  log_path=directory / "run.log", timeout=lane.get("timeout"), on_stop=on_stop))
            else:
                jobs = 1 if args.serial else lane.get("jobs", len(group))
                receipt.update(parallel=group, jobs=jobs)
                members = [(name, config["lanes"][name]) for name in group]
                def execute(on_stop=None):
                    return record(directory, receipt, lambda: parallel.lanes(root, members, directory / "lanes", jobs,
                                  lambda member, status, log: judge(member, status, log)[0], receipt, on_stop))
            try:
                with processes.forwarding_signals():
                    if resource == "light": result = execute()
                    else:
                        with contextlib.ExitStack() as stack:
                            try:
                                admission = stack.enter_context(resources.Admission(args.lane,
                                    exclusive=resource == "exclusive", timeout=FREE_WAIT if args.if_free else 5400,
                                    model=lane.get("model", False)))
                            except TimeoutError as error:
                                # Admission published nothing, so declining leaves nothing to release.
                                if not args.if_free: raise
                                print(f"eng: {args.lane} not run: {error}", file=sys.stderr)
                                return BUSY
                            result = execute(admission.interrupted)
            except processes.TimedOut as error:
                # The lane's timeout is its backstop, so a run a busy host slowed past it is
                # unmeasured (guides/measurement.md). Its log is incomplete and counts nothing.
                print(f"eng: unmeasured: {error}", file=sys.stderr)
                return evidence.UNMEASURED
            # A group's lanes were each judged by their own logs.
            if "parallel" in lane:
                conclude(directory, receipt, result)
                return result
            # The command has a confirmed terminal result. Coverage validation cannot turn
            # that completed process into an orphan reservation.
            log = (directory / "run.log").read_text(errors="replace")
            try:
                status, unmeasured = judge(lane, result, log, extra, args.expect)
            except ValueError as error:
                conclude(directory, receipt, 2, error=str(error))
                raise
            conclude(directory, receipt, status, **({"unmeasured": unmeasured} if status == evidence.UNMEASURED else {}))
            return status
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"eng: {error}", file=sys.stderr)
        return 2
