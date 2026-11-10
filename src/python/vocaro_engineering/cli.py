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
import time

from . import __version__, app_builds, app_registrations, budgets, checkpoint, crash_alerts, device_pass, devices, disk, docs, evidence, evidence_offload, failure_records, foreground, github_artifacts, keychain, lane_coverage, lane_report, lane_wait, merge_queue, run_results, \
    notification_center, offload_host, parallel, processes, release, remote, resources, setup, skills, spelling, stale_modules, storekit_config, toolkit_copy, xctest_shards
from .config import TOOLKIT_ROOT, CAPABILITIES, DEFAULT_TOOLKIT, admission as admission_of, lane_to_run, load, source_identity, \
    display_lane_warnings, lane_devices, self_lane_warnings, stays, unadmitted, unlocked_screen
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
# Commands that start a background retry of evidence offloads that found the second Mac unreachable.
OFFLOAD_RETRIES = {"run", "admit", "remote", "land", "queue", "disk"}
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


# The files init copies into a project, by the toolkit file each one copies.
COPIES = {"scripts/eng": "integrations/eng.py", "scripts/_engineering.py": "integrations/shared.py"}


def stale_copies(root):
    """Warnings for a project's launcher and adapter that differ from this toolkit's. The
    toolkit's own checkout, which has integrations/, keeps a launcher of its own."""
    if (root / "integrations/shared.py").is_file():
        return []
    # Either checkout may be the one behind; copying from a toolkit behind its origin installs an older file.
    return [f"{copy} differs from the toolkit's {source}: pull whichever checkout is behind its origin, and if "
            f"they still differ, replace it with `cp {TOOLKIT_ROOT / source} {root / copy}` (guides/adoption.md)"
            for copy, source in COPIES.items()
            if (root / copy).is_file() and (root / copy).read_bytes() != (TOOLKIT_ROOT / source).read_bytes()]


def fix_hosts(root, hosts):
    """What `eng doctor` fixes of remote.compare_hosts' report itself, the owner's no longer (the owner's
    decision, 2026-11-03): runtimes that differ, which it evens out (remote.match_runtimes) and then
    compares again, and stale pool simulators on both Macs, which it shuts down (simulators.pool.reap_reports)
    and lists in reaped, and Xcode's default simulators on this Mac, which it deletes as `eng disk --delete`
    does, checking each again first, and lists in deletedDefaults, and the second Mac's own toolkit checkout,
    which it fast-forwards to origin/main when it can and reports in toolkitCheckout, an error when it cannot
    (remote.update_there, vocaro-engineering#203). Its progress goes to standard error, so
    that standard output stays the report. tests/host_isolation.py replaces it, so that no test changes
    either Mac."""
    if hosts.pop("runtimesDiffer", False):
        with contextlib.redirect_stdout(sys.stderr): remote.match_runtimes(hosts["host"], check=False)
        hosts = remote.compare_hosts()
        hosts.pop("runtimesDiffer", None)
    # The second Mac only when it answered the comparison: one away is no error, and is not waited for.
    reaped = simulator_pool.reap_reports(None, root, dry_run=False, here="remote" not in hosts)
    # With each held entry whose process is gone that reap could not release, and why, so that no leftover
    # waits unseen behind an empty leftovers (vocaro-engineering#156).
    hosts["reaped"] = [line for where, report in reaped
                       for line in remote.reap_lines(where, report, listing=False, held=True)]
    hosts["deletedDefaults"] = delete_defaults()
    # The second Mac's own toolkit checkout, which it fast-forwards to origin/main when it can; one it must
    # leave as it is is an error, the owner's to decide (vocaro-engineering#203).
    if "remote" in hosts:
        found = hosts["toolkitCheckout"] = remote.update_there(hosts["host"])
        if found.get("state") == "unchecked":
            hosts.setdefault("unchecked", []).append(f"{hosts['host']}'s own toolkit checkout, which it did not "
                                                     f"report: {found.get('reason')}")
        else:
            line, owner = remote.toolkit_line(hosts["host"], found)
            if line: found["said"] = line
            if owner: hosts.setdefault("errors", []).append(line)
            elif line: print(f"remote: {line}", file=sys.stderr, flush=True)
    return hosts


def delete_defaults(run=subprocess.run):
    """Delete Xcode's default simulators on this Mac that `eng disk` finds deletable, each checked again
    first and recorded in its ledger, and return what was said of each."""
    def defaults():
        found = disk.Host.current(0, None, run)
        return found, [item for item in disk.inventory(found, disk.Sizes(run, False), ["simulator"])
                       if item.status == disk.CANDIDATE and item.reason.startswith("Xcode's default device")]
    try:
        host, items = defaults()
        if not items: return []
        said = []
        disk.delete(items, lambda category: defaults()[1], disk.ledger_path(host.home), out=said.append, run=run)
        return said
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        return [f"could not delete Xcode's default simulators: {type(error).__name__}: {error}"]


def default_simulators(config):
    """A warning when Xcode's default simulators exist on this Mac and the project uses simulators.

    A script or Xcode run that picks a device by name, or `booted`, finds one of them instead of a
    pool member and fills it with the project's apps (vocaro-engineering#91). Xcode makes them again
    when it installs a runtime. `eng doctor` deletes each one first (fix_hosts), so this names only
    those still there: booted, in use or named by a process."""
    if not any(capability.startswith("simulator") for capability in config.get("capabilities", [])):
        return []
    try:
        result = simulator_pool.simctl("list", "devices", "-j", timeout=120)
        found = simulator_pool.listed_defaults(json.loads(result.stdout)) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return []
    if not found:
        return []
    shown = ", ".join(found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")
    return [f"{len(found)} of Xcode's default simulators exist on this Mac: {shown}. A script or Xcode run that "
            "picks a device by name or `booted` installs on one instead of a pool member; `eng doctor` deletes each "
            "one that is shut down and unused, and `eng disk` says why it kept these (guides/simulator-testing.md, "
            "Reserved simulator pool)"]


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
        agents.write_text("# Contributor instructions\n\n" + (TOOLKIT_ROOT / SHARED_GUIDANCE).read_text() + "\n"
                          + "".join(f"{line}\n" for line in claude_imports(load(root)))
                          + "\nRecord project-specific product rules and justified exceptions here.\n")
    # Claude Code loads an imported AGENTS.md at session start, and the imports in it; it may not follow a link.
    bridge = root / "CLAUDE.md"
    if not bridge.exists(): bridge.write_text(f"{CLAUDE_ENTRY}\n")
    load(root)
    print(f"Initialized {root}; existing instructions and commands were preserved")


# The pointer every direct-main project's AGENTS.md holds word for word. The rules stay in the
# toolkit: AGENTS.md imports them for Claude Code, and the pointer sends any other agent to eng instructions.
SHARED_GUIDANCE = "agents/shared-guidance.md"
# A project's CLAUDE.md holds this line alone, beside any comment lines (owner's decision, 2026-11-01).
CLAUDE_ENTRY = "@AGENTS.md"
# The toolkit's main checkout, under the home path both Macs share, which AGENTS.md imports from.
SHARED_TOOLKIT = "~/Development/vocaro-engineering"


def claude_imports(config):
    """The lines a project's AGENTS.md imports: the common rules and each enabled capability's.
    Claude Code follows them through CLAUDE.md's import of AGENTS.md, so every Claude Code agent, a
    subagent included, has them without running eng instructions; other agents read them as text
    beside the pointer. The toolkit is named at SHARED_TOOLKIT, under the home folder both Macs share,
    because a path relative to the project does not reach it from a worktree under .claude/worktrees.
    It is never the checkout running eng: a queue gate on the second Mac runs from a toolkit checkout
    of its own, which no project names (#106)."""
    return [f"@{SHARED_TOOLKIT}/agents/core.md",
            *(f"@{SHARED_TOOLKIT}/agents/capabilities/{name}.md" for name in config.get("capabilities", []))]


def claude_entry_lines(text):
    """CLAUDE.md's lines other than blank lines and HTML comments, which may span lines."""
    return [line.strip() for line in re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines() if line.strip()]


def agent_entry(root, config):
    """Problems with a direct-main project's AGENTS.md and CLAUDE.md: the shared section copied
    other than word for word, an import of the common rules missing from AGENTS.md, or a CLAUDE.md
    that holds more than `@AGENTS.md`. The toolkit's own checkout, which has integrations/, keeps
    instructions of its own."""
    if (root / "integrations/shared.py").is_file() or config.get("integration", "direct-main") != "direct-main":
        return []
    problems = []
    agents = root / "AGENTS.md"
    text = agents.read_text() if agents.is_file() else ""
    if (TOOLKIT_ROOT / SHARED_GUIDANCE).read_text() not in text:
        problems.append(f"AGENTS.md does not hold the toolkit's {SHARED_GUIDANCE} word for word: put that section "
                        "in place of its own, and keep this project's rules after it (guides/adoption.md)")
    lines = text.splitlines()
    missing = [line for line in claude_imports(config) if line not in lines]
    if missing:
        problems.append(f"AGENTS.md does not import {', '.join(missing)}: add each on a line of its own after the "
                        "shared section (guides/adoption.md)")
    claude = root / "CLAUDE.md"
    if claude_entry_lines(claude.read_text() if claude.is_file() else "") != [CLAUDE_ENTRY]:
        problems.append(f"CLAUDE.md does not hold {CLAUDE_ENTRY} alone: make it that line, keeping any comment "
                        "lines, and move any other import into AGENTS.md (guides/adoption.md)")
    return problems


def split_warnings(root, config):
    """The gate over its budget and main past its release commit limit, as eng land warns after its result,
    in a project with a Git repository (budgets.py)."""
    try:
        common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if common.returncode: return []
    return budgets.landing_warnings(root, config, budgets.entries(Path(common.stdout.strip()) / "eng-queue/entries"))


def behind_main(root):
    """How many commits of origin/main, as last fetched, the checkout at root lacks. It never fetches."""
    try:
        count = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD..refs/remotes/origin/main"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    return int(count.stdout) if count.returncode == 0 else 0


# The start of the last line eng instructions prints, which its first line tells the reader to look for.
INSTRUCTIONS_END = "End of instructions"


def instructions(config):
    """Print the common rules, the enabled capabilities and the project's exceptions, between a first line that gives
    their length and a last line that says they ended. A cut read loses the last line, and only the first line, which
    every read keeps, can say that line should be there: `| head -150` once lost most of core.md unnoticed (#294)."""
    parts = []
    behind = behind_main(TOOLKIT_ROOT)
    if behind:
        parts.append(f"Note: the toolkit checkout {TOOLKIT_ROOT} lacks {behind} commit(s) of origin/main as last fetched, "
                     f"so these rules may be out of date. Print the current ones with `git -C {TOOLKIT_ROOT} show "
                     "origin/main:agents/core.md`, or update the checkout (guides/adoption.md, Updating and rolling back).\n")
    parts.append((TOOLKIT_ROOT / "agents/core.md").read_text())
    for capability in config.get("capabilities", []):
        parts.append((TOOLKIT_ROOT / "agents/capabilities" / f"{capability}.md").read_text())
    for exception in config.get("exceptions", []):
        parts.append(f"Project exception — {exception['rule']}: {exception['reason']}")
    body = "".join(f"{part}\n" for part in parts)
    # The first line, the blank line after it and the last line, beside the body's own.
    lines, size = body.count("\n") + 3, round(len(body.encode()) / 1024)
    print(f'Read all of this: {lines} lines, about {size} KB, and the last line starts "{INSTRUCTIONS_END}". Without '
          "that line you were shown only part, and the rest still applies: write the output to a file and read every "
          f"part.\n\n{body}{INSTRUCTIONS_END}: {lines} lines, about {size} KB.")


class PolicyUnavailable(RuntimeError):
    """The toolkit's main could not be fetched, which says nothing of the work: unmeasured, not failed."""


# The tries of the policy's fetch, and the seconds between them. Runs that start together fetch into one toolkit
# checkout, as a bisect's two halves do (merge_queue.Beside): when main has moved, all but one of them fail to lock
# origin/main, which the first has already moved, and a second try finds it current (vocaro-engineering#249).
POLICY_TRIES, POLICY_RETRY_SECONDS = 3, 0.5


def refresh_policy(root, stream=None):
    for tried in range(1, POLICY_TRIES + 1):
        try:
            subprocess.run(["git", "-C", str(TOOLKIT_ROOT), "fetch", "origin",
                            "refs/heads/main:refs/remotes/origin/main"], check=True, timeout=120)
            break
        except subprocess.CalledProcessError as error:
            if tried == POLICY_TRIES:
                tries = f" in {tried} tries" if tried > 1 else ""
                raise PolicyUnavailable(f"the current policy could not be fetched{tries}, so nothing ran: "
                                        f"{error}") from error
            print(f"eng: the policy's fetch exited {error.returncode}, perhaps beside another run's fetch: trying "
                  f"again ({tried + 1} of {POLICY_TRIES})", file=sys.stderr, flush=True)
            time.sleep(POLICY_RETRY_SECONDS)
        except subprocess.SubprocessError as error:
            # A fetch that ran out of time is not retried, so that an unreachable origin costs one wait, not three.
            raise PolicyUnavailable(f"the current policy could not be fetched, so nothing ran: {error}") from error
    commit = subprocess.check_output(["git", "-C", str(TOOLKIT_ROOT), "rev-parse", "origin/main"], text=True).strip()
    text = subprocess.check_output(["git", "-C", str(TOOLKIT_ROOT), "show", f"{commit}:policies/shared-host.md"], text=True)
    # Flushed now, or a redirected stdout holds it until eng exits, after all the work's output (#274).
    print(f"Policy source: {commit}\n\n{text}", file=stream, flush=True)
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


def compile_failed(receipt):
    """Whether the run's "compileFirst" step ended with a failed build."""
    return receipt.get("compile", {}).get("outcome") == "failed"


def compile_first(lane, root, directory, receipt, limit, env, on_stop, kill_after):
    """A lane's "compileFirst" command, run inside the lane's one admission and environment, so that its xcodebuild or
    swift has the stale-module rebuild the lane's own has (stale_modules.py). Returns (status, seconds left): the
    status of a failed build with None, its output staying in run.log, or None with what remains of limit for the
    lane's command, which shares the lane's one timeout. The step's output moves to compile.log once it has passed, so
    that run.log holds only the tests eng reads. A build that fails never reads as unmeasured, whatever status it
    exited with; one that reaches the limit raises TimedOut, which is. A lane without the step has its whole limit."""
    compiled = directory / "compile.log"
    compiled.unlink(missing_ok=True)
    command = lane.get("compileFirst")
    if command is None: return None, limit
    log = directory / "run.log"
    step = receipt["compile"] = {"command": command}
    began = time.monotonic()
    status = processes.run(command, cwd=root, log_path=log, timeout=limit, on_stop=on_stop, env=env, kill_after=kill_after)
    seconds = time.monotonic() - began
    step.update(exitStatus=status, seconds=round(seconds, 3))
    if status == 0:
        os.replace(log, compiled)
        step.update(outcome="passed", log=compiled.name)
        return None, max(limit - seconds, 1)
    step.update(outcome="failed", log=log.name)
    with log.open("a") as file:
        print(f"eng: the build failed: compileFirst exited {status}, and no test ran", file=file)
    print(f"eng: the build failed: compileFirst exited {status}, and no test ran; its log is {log}", file=sys.stderr, flush=True)
    return (1 if status == evidence.UNMEASURED else status), None


def conclude(directory, receipt, status, **fields):
    """Record a completed run's verdict, the status eng run returns, beside its command's exit
    status: passed only for 0, so a zero exit that ran too few tests is failed."""
    outcome = "passed" if status == 0 else "unmeasured" if status == evidence.UNMEASURED else \
        "build-failed" if compile_failed(receipt) else "failed"
    receipt.update(outcome=outcome, **fields)
    (directory / "source.json").write_text(json.dumps(receipt, indent=2) + "\n")


def judge(lane, result, log, extra=(), expect=None):
    """A lane's status and unmeasured tests from its command's status and its log. A completed run
    that executed too few tests raises ValueError."""
    measured = result in (0, evidence.UNMEASURED)
    not_run = []
    repeats = evidence.repeats_asked(extra)
    # --expect counts one run's tests, and a series' log adds up all of its runs.
    expect = expect * repeats if repeats and expect else expect
    if repeats is not None and measured and evidence.series_reported(log) != repeats:
        # The command ignored --repeat, or stopped before its series ended: however it ended, the series did not happen.
        cause = f"--repeat {repeats} was asked for, and the lane's log reports no series of {repeats} runs"
        print(f"eng: unmeasured: {cause}", file=sys.stderr)
        return evidence.UNMEASURED, [cause]
    if measured and (lane.get("minimumTests") or expect):
        # Arguments such as a filter select part of the lane, which must still run a test.
        minimum = 1 if extra or expect else lane["minimumTests"]
        try:
            evidence.require_tests(log, minimum, expected=expect)
        except evidence.NotRun as error:
            # Such as a corpus test canceled for a variable this lane's command does not set on its Mac
            # (vocaro-engineering#285). The lines give each test's reason: the cause an unmeasured run records.
            not_run = [str(error), *evidence.canceled_tests(log)]
            print("eng: unmeasured:", *not_run, sep="\n", file=sys.stderr)
        except ValueError as error:
            # An unmeasured part may not have run at all, so the run stays unmeasured. A test the
            # host left unmeasured ran nothing, so it can also be what leaves a run short.
            if result == 0 and not evidence.unmeasured_tests(log): raise
            print(f"eng: {error}", file=sys.stderr)
    unmeasured = evidence.unmeasured_tests(log) if measured else []
    if unmeasured or not_run or result == evidence.UNMEASURED:
        # A skip or cancellation leaves the command's own status at 0, and unmeasured is never green.
        if unmeasured:
            print(f"eng: unmeasured: {len(unmeasured)} test(s) reported unmeasured:", *unmeasured,
                  sep="\n", file=sys.stderr)
        return evidence.UNMEASURED, list(dict.fromkeys([*not_run, *unmeasured]))
    return result, []


# A lane that says "display": true waits for what the display's refresh times, such as a scroll view's
# animation. Its command runs under a one-second caffeinate -u, which wakes a sleeping display, behind a
# lock too, and then caffeinate -d, which keeps an awake one on but wakes none, and no other assertion
# (guides/background-mac-testing.md, Lock screen and display sleep).
AWAKE = ["sh", "-c", 'caffeinate -u -t 1; exec caffeinate -d -- "$@"', "sh"]


def awake(lane, extra=()):
    """The command a lane runs, with extra appended: under AWAKE when the lane says "display": true,
    where the receipt's command shows the assertions. caffeinate -d execs the command in its own
    process and holds the assertion from a child of it, which ends when the command does, so the
    command keeps the run's PID and status and the assertion ends with it, however it ends (this Mac,
    macOS 27.0.1, 2026-10-28)."""
    command = [*lane["command"], *extra]
    return [*AWAKE, *command] if lane.get("display") else command


def lane_label(root, config, lane):
    """A lane run's admission label: its project's name, a space and the lane, such as `EPUBLib gate`,
    so that two projects' lanes of one name stay apart in waiting messages, the guard and the admission
    log (#51). A space, as in the labels callers pass, such as `warm push REV`, and in an `eng admit`
    command line; no configured project name or lane has one, and a colon would read as one more field
    after the `: ` that waiting messages put before a label. The name is engineering.json's, else the
    checkout's; a linked worktree's directory is named for its session, so it takes its main checkout's."""
    name = config.get("name") or (project_adapter(TOOLKIT_ROOT).main_checkout(root) or root).name
    return f"{name} {lane}"


def placement(reason):
    """Where a run ran, for its receipt, so that the share of heavy work each Mac ran can be counted:
    ranOn, `this Mac` or `second Mac`, the name of the Mac, and on this Mac hereBecause, why the run
    ran here rather than on the second Mac. Work the second Mac runs for this Mac says so, and names
    the Mac that sent it (remote.DISPATCHER)."""
    sender = os.environ.get(remote.DISPATCHER)
    if sender: return {"ranOn": "second Mac", "mac": offload_host.mac_name(), "dispatchedFrom": sender}
    return {"ranOn": "this Mac", "mac": offload_host.mac_name(), "hereBecause": reason}


def admission_placement(offered, reason):
    """What a run's admission line says of its placement, which `eng admissions usage` counts: whether eng offered it
    to the second Mac first (offered) and why it runs here (because), or the Mac that sent it (remote.DISPATCHER)."""
    sender = os.environ.get(remote.DISPATCHER)
    if sender: return {"dispatchedFrom": sender}
    # With no second Mac configured, Offer.make gives that reason before checking local eligibility.
    declined = offered is not None and (offered.host is None or offered.refused is None)
    return {"offered": declined, "because": reason}


def this_mac():
    """The Mac this eng runs on, as messages name it: `this Mac (NAME)`, or `the second Mac (NAME)` for work the
    second Mac runs for the Mac that sent it (remote.DISPATCHER), so that a log never leaves the Mac to be guessed
    (vocaro-engineering#185)."""
    return f"{'the second Mac' if os.environ.get(remote.DISPATCHER) else 'this Mac'} ({offload_host.mac_name()})"


# Seconds between readings of this Mac's screen while a lane that needs it unlocked waits for it.
SCREEN_POLL = 30
# A test's own report that a lock left it unmeasured, such as Pagepour's `Unmeasured: the screen is locked`.
SCREEN_LOCKED = re.compile(r"(?i)\bscreen is locked\b")


def screen_reading(stream=None):
    """This Mac's screen, read now (offload_host.screen) and said in one line naming the Mac (this_mac)."""
    found = offload_host.screen()
    print(f"eng: {offload_host.screen_line(found, this_mac())}", file=stream or sys.stderr, flush=True)
    return found


def scroll_bars_recorded(receipt, lane, inherited):
    """Record in a lane's receipt which scroll-bar kind this Mac gives as it starts, and say so in one line naming the
    Mac (offload_host.scroll_bars, vocaro-engineering#229). A lane's log says it once for its Mac: work nested in
    an admission or a self lane says nothing of its own, nor does the provision lane a dispatch runs before its work,
    though each still records the reading."""
    found = receipt["scrollBars"] = offload_host.scroll_bars()
    if inherited or os.environ.get(SELF_LANE) or (lane == "provision" and os.environ.get(remote.DISPATCHER)): return
    print(f"eng: {offload_host.scroll_bars_line(found, this_mac())}", file=sys.stderr, flush=True)


def screen_wait(name, seconds, said):
    """None once this Mac's screen reads unlocked, within seconds, for a lane that needs it unlocked; otherwise why
    not, after the wait. Each new reading that refuses is said once (said keeps the last); the lane waits instead
    of running unmeasured (vocaro-engineering#185)."""
    deadline = time.monotonic() + max(seconds, 0)
    while True:
        found = offload_host.screen()
        if found["state"] == "unlocked": return None
        refused, key = remote.screen_refusal(found, this_mac()), (found["state"], found.get("reason"))
        lane_wait.waiting(lane_wait.SCREEN, refused)
        if not said or said[-1] != key:
            print(f"eng: {name} waits: {refused}", file=sys.stderr, flush=True)
            said.append(key)
        left = deadline - time.monotonic()
        if left <= 0: return refused
        time.sleep(min(SCREEN_POLL, left))


def admitted_here(stack, name, label, timeout, offered=None, once=False, exclusive=False, model=False, whole_host=False,
                  screen=False, placement=None):
    """This Mac's heavy-work admission of the work label names, entered in stack, or the work's status
    instead: its status on the second Mac when the second Mac took it after all, MISCONFIGURED, or
    BUSY when no admission came, after saying so. Work the second Mac did not take (offered), declined
    as busy or not offered while it had no spare capacity and this Mac had, goes to whichever Mac admits
    it first, this Mac asked first (remote.overflow). Other work waits in this Mac's queue for timeout
    seconds, or at most FREE_WAIT with once. A whole-host lane takes every guaranteed slot,
    and keeps one place in this Mac's queue across its waits until it no longer waits here (resources.Place). A
    lane that needs an unlocked screen (screen) is admitted only while this Mac's screen reads unlocked, and waits
    for it as for a slot (screen_wait). Its admission line says placement (admission_placement)."""
    refused, said = [], []
    place = resources.Place() if whole_host and not exclusive else None
    # Its own place is no request queued ahead of it when eng reads this Mac's capacity for it (#186).
    if offered is not None: offered.place = place

    def admit(seconds):
        if screen:
            began = time.monotonic()
            locked = screen_wait(name, seconds, said)
            if locked is not None:
                refused.append(locked)
                return None
            seconds = max(seconds - (time.monotonic() - began), 0)
        try:
            # eng admit and eng run start every command they admit through processes.supervise (#315).
            return stack.enter_context(resources.Admission(label, exclusive=exclusive, timeout=seconds, model=model,
                                                           whole_host=whole_host, place=place, placement=placement,
                                                           supervised=True))
        except TimeoutError as error:
            # Admission published nothing but the place, which the finally below leaves.
            refused.append(error)
            return None
    try:
        if offered is not None and offered.busy is not None:
            return remote.overflow(offered, admit, timeout, once, refused)
        # A wait that runs out is BUSY, as with once: nothing ran, which the merge queue counts as no verdict.
        # Raised, TimeoutError is an OSError, which eng run reports as its own error, exit 2 (#268).
        admission = admit(min(FREE_WAIT, timeout) if once else timeout)
        if admission is None:
            print(f"eng: {name} not run on {this_mac()}: {refused[-1]}", file=sys.stderr)
            return BUSY
        return admission
    except resources.AdmissionRecordError as error:
        print(f"eng: {name} not run on {this_mac()}: {error}", file=sys.stderr)
        return BUSY
    finally:
        # Admitted, admission took the place out of the queue; otherwise the work no longer waits here.
        if place is not None: place.leave()


def foreground_held(stack, name, label, timeout, **options):
    """None once the second Mac's foreground is held for the work label names, entered in stack; or BUSY, after saying
    so, when the wait for it ran out, as admitted_here's does (#297). Nothing ran, and the TimeoutError raised, an
    OSError, would be eng's own error, exit 2, which the merge queue counts as a failure. Only entering is caught: a
    release that fails once the work has run does not make the work not run."""
    try:
        stack.enter_context(foreground.slot(label, timeout=timeout, **options))
    except TimeoutError as error:
        print(f"eng: {name} not run on {this_mac()}: {error}", file=sys.stderr)
        return BUSY
    return None


def window_wait(name, window, label):
    """How long work on the devices of window may wait for admission, a devices.WindowWait, or None for work with no
    window. It is said at the start, since it decides when the work stops waiting (vocaro-engineering#292). label is
    the admission's, whose runs in the admission log measure the work; None for work no earlier run measures."""
    if window is None: return None
    waiting = devices.WindowWait(window, resources.measured_seconds(label) if label is not None else None)
    if waiting.left() > 0: print(f"eng: {name} waits for admission at most until {waiting.latest.isoformat()}: {waiting}",
                                 file=sys.stderr, flush=True)
    return waiting


def window_closed(name, waiting):
    """BUSY, after saying so, once the last start that can finish within the device window has passed for work whose
    measured time says it cannot finish, or None: such work does not start, and nothing ran. Work no run has measured
    waits for nothing then, and if admitted at once meets the bound its reservation sets, which refuses or stops it."""
    if waiting is None or waiting.measured is None or waiting.left() > 0: return None
    print(f"eng: {name} not run on {this_mac()}: {waiting}. No device result was recorded", file=sys.stderr, flush=True)
    return BUSY


def not_admitted(name, waiting, status):
    """status, the end of a wait for admission, after saying that the device window is what cut it short."""
    if waiting is not None and waiting.limited and status == BUSY:
        print(f"eng: {name} stopped waiting: {waiting}. No device result was recorded", file=sys.stderr, flush=True)
    return status


def fell_back(name, offered):
    """Say in one line, before the run starts, that work offered to the second Mac runs on this Mac, and why, so
    that the second Mac's lines a declined offer left above are not read as the run's (vocaro-engineering#185)."""
    above = "; the second Mac's lines above are from the offer it did not take" if offered.sent else ""
    print(f"eng: {name} runs on {this_mac()}, not the second Mac: {offered.status}{above}", file=sys.stderr, flush=True)


def unmeasured_screen(name, receipt):
    """Read the screen again once a lane whose result depends on it ends unmeasured, and say on which Mac, so
    that the verdict and the hold it leads to name the Mac whose screen was read (vocaro-engineering#185)."""
    print(f"eng: {name} unmeasured on {this_mac()}", file=sys.stderr, flush=True)
    receipt["screenAfter"] = screen_reading()


SCREEN_STATUS = {"unlocked": 0, "locked": 1, "unknown": 2}


def host_screen(args):
    """`eng host screen [--remote]`: one line, or the reading as JSON, saying whether this Mac's or the second
    Mac's console screen is locked (offload_host.screen), exiting 0 unlocked, 1 locked and 2 unknown. The
    second Mac is read over SSH by the same code, so either Mac's answer is read, never assumed."""
    if args.host and not args.remote: raise ValueError("--host names the second Mac that --remote reads")
    if args.remote:
        host = args.host or remote.configured()
        if host is None:
            raise ValueError(f"no second Mac is configured ({remote.HOST_VARIABLE}, or the ssh alias "
                             f"{' or '.join(remote.ALIASES)} in ~/.ssh/config)")
        try:
            found = remote.screen_there(host)
        except remote.Unavailable as error:
            found = {"mac": host, "at": time.strftime("%H:%M:%S"), "state": "unknown",
                     "reason": f"{host} could not be read: {getattr(error, 'problem', None) or error}"}
        if not isinstance(found, dict) or found.get("state") not in SCREEN_STATUS:
            found = {"mac": host, "at": time.strftime("%H:%M:%S"), "state": "unknown",
                     "reason": f"{host} answered {found!r}"}
        where = f"the second Mac ({found.get('mac')})"
    else:
        found, where = offload_host.screen(), this_mac()
    print(json.dumps(found, indent=2) if args.json else offload_host.screen_line(found, where))
    return SCREEN_STATUS[found["state"]]


# Set to off, work that runs on this Mac does not reap first (reap_here). tests/host_isolation sets it, so that no
# run a test makes, in its process or a child, stops a simulator or releases a reservation of this Mac.
REAP_SWITCH = "VOCARO_REAP_BEFORE_RUN"


def reap_here(root, inherited):
    """Before heavy work runs on this Mac, the reap that runs before work is sent to the second Mac (remote.execute):
    the stale simulators of the project's pool and of the pools its checkouts name shut down, and the pool
    reservations reap proves dead released (offload_host.reap, remote.reap_request), within REAP_SECONDS. Each stop,
    failure, release and problem goes to standard error as a dispatch says them (remote.reap_lines), and the work
    runs all the same; the lines are returned. Work a dispatch sent here was reaped by that dispatch, and work that
    inherited an admission (inherited) by the run it is nested in, so neither reaps again (vocaro-engineering#156)."""
    if os.environ.get(REAP_SWITCH, "").strip().lower() == "off": return []
    if inherited or os.environ.get(remote.DISPATCHER): return []
    # offload_host.reap never raises; it says what it could not read or do in its report's problem.
    report = offload_host.reap(remote.ROOT, **remote.reap_request(root))
    lines = remote.reap_lines(remote.HERE, report, listing=False)
    for line in lines: print(f"eng: {line}", file=sys.stderr, flush=True)
    return lines


def admitted_tree(root, name, offered, receipt):
    """None when work this Mac admitted may run, after recording in receipt the tree it reads, or 2 after
    saying why not. A run here reads the working tree as it is once admitted, not as it was when the
    work was offered or queued: work this Mac takes after sending the second Mac a commit runs only
    while the tree is still that commit's (remote.changed_since), and receipt's sent names that commit."""
    changed = remote.changed_since(root, offered)
    if changed is not None:
        print(f"eng: {name} not run on this Mac: {changed}\neng: restore the tree that was sent, or run it again to "
              "send the tree as it is now", file=sys.stderr, flush=True)
        return 2
    receipt["project"] = source_identity(root)
    if offered is not None and offered.sent.get("commit"): receipt["sent"] = offered.sent["commit"]
    return None


def from_sent(stack, root, sent, moved, receipt):
    """(checkout, toolkit) of the worktrees from which `eng admit` runs its command on this Mac, entered in stack,
    after recording them in receipt; or 2 after saying why not. A command this Mac takes after sending the second
    Mac a commit runs from that commit once the tree here is no longer that commit's (remote.moved_trees), in a
    temporary worktree at it, as the second Mac runs it, rather than from the changed tree or not at all: the
    session may go on editing meanwhile, and the result is that of what was sent (vocaro-engineering#276, whose
    command waited behind a gate here and then refused over an unrelated documentation edit). The worktrees go
    when the command ends, however it ends (remote.sent_tree)."""
    why = "; ".join(f"the {what} changed after {commit[:12]} was sent to the second Mac:\n{changes}"
                    for _checkout, commit, what, changes in moved)
    try:
        checkout, toolkit = stack.enter_context(remote.sent_tree(
            root, sent, any(what.startswith("toolkit") for _checkout, _commit, what, _changes in moved)))
    except (OSError, RuntimeError) as error:
        print(f"eng: the command not run on this Mac: {why}\neng: and no worktree of what was sent could be made "
              f"here: {error}", file=sys.stderr, flush=True)
        return 2
    print(f"eng: the command runs on this Mac from what was sent, {sent['commit'][:12]}, in a temporary worktree, "
          f"{checkout}, since {why}\neng: the session's tree stays as it is, and the worktree goes when the command "
          "ends", file=sys.stderr, flush=True)
    receipt["project"] = source_identity(checkout)
    receipt["sent"] = sent["commit"]
    receipt["sentTree"] = {"checkout": str(checkout), "toolkit": str(toolkit),
                           "moved": [{"tree": what, "commit": commit, "changes": changes}
                                     for _checkout, commit, what, changes in moved]}
    return checkout, toolkit


def matching_directory(root):
    """The current directory relative to the checkout root's top level, which the command runs in: "." outside it."""
    top = Path(remote.git(root, "rev-parse", "--show-toplevel")).resolve()
    try:
        return Path.cwd().resolve().relative_to(top)
    except ValueError:
        return Path(".")


def provision_sent(root, config, checkout, toolkit, variables, limit, on_stop=None):
    """0 once the project's provision lane, if it has one, has run in the worktree of what was sent, as it runs in
    the second Mac's checkout before the command; otherwise its status, after saying where its log is."""
    if "provision" not in config.get("lanes", {}): return 0
    log = Path(root) / ".build/engineering/admit/provision.log"
    status = processes.run([sys.executable, str(toolkit / "bin/eng"), "--project", str(checkout), "run", "--here",
                            "provision"], cwd=checkout, log_path=log, timeout=limit, env=variables,
                            on_stop=on_stop)
    if status:
        print(f"eng: the command not run: the provision lane exited {status} in the worktree of what was sent; its "
              f"log is {log}", file=sys.stderr, flush=True)
    return status


def guarded_alerts(stack, purpose, receipt, apps=()):
    """Foreground work's crash_alerts.Run, with crash-report dialogs off and the screen's crash-report alerts
    cleared until the stack closes; or UNMEASURED, with nothing run, when an alert eng cannot clear is over the
    screen (crash_alerts.py). apps names the app under test, as a crash-report alert names its process."""
    try:
        return stack.enter_context(crash_alerts.guarded(purpose, receipt=receipt, apps=apps))
    except crash_alerts.Blocked as error:
        print(f"eng: unmeasured: {error}; nothing ran ({crash_alerts.GUIDE}, Crash-report alerts)", file=sys.stderr)
        receipt["outcome"] = "unmeasured"
        return evidence.UNMEASURED


def cleared_notifications(receipt):
    """None once no Notification Center window is over the second Mac's screen, eng having closed the
    notifications of any that was, under the foreground the run holds; or UNMEASURED, with nothing run, naming
    a window it could not clear (notification_center.py). The receipt says what was found and closed, never
    what a notification said."""
    try:
        receipt["notificationCenter"] = notification_center.dismiss()
        return None
    except (notification_center.Blocked, devices.Refused) as error:
        print(f"eng: unmeasured: {error}; nothing ran ({notification_center.GUIDE}, {notification_center.SECTION})",
              file=sys.stderr)
        receipt["notificationCenter"] = {"left": str(error)}
        receipt["outcome"] = "unmeasured"
        return evidence.UNMEASURED


def here_because(args, inherited):
    """Why `eng admit` runs its command on this Mac without offering it to the second Mac, or None to offer it there
    first, as a lane is (the owner's decision, 2026-11-07; guides/shared-host.md, Which Mac runs a lane)."""
    if args.here: return "eng admit --here"
    # Work the second Mac runs for the Mac that sent it runs there, where it was sent.
    if os.environ.get(remote.DISPATCHER): return "sent here by the Mac that dispatched it"
    # Foreground work holds the foreground of the Mac it runs on (foreground.py).
    if args.foreground: return "foreground work runs on the Mac whose foreground it holds"
    if inherited: return "nested in the admission it inherits"
    # The parts of a self lane share build products, so all run on the Mac where the lane runs (SELF_LANE).
    lane = os.environ.get(SELF_LANE)
    if lane: return f"a part of the self lane {lane}, whose parts share build products on the Mac where it runs"
    return None


# Set by eng run for the command of a self lane, to the lane's name: each `eng admit` its script runs stays on the
# Mac where the lane runs, since one part builds what the next runs (config.unadmitted).
SELF_LANE = "VOCARO_SELF_LANE"


@contextlib.contextmanager
def patch_environment(values):
    """os.environ with values set, for the commands started meanwhile, and as it was afterwards."""
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def admit(root, args, config=None, *, device_window=None):
    # A command goes to whichever Mac has spare capacity for it, the second Mac first, as a lane does, and runs there
    # in the checkout's directory that matches the current one; what it changed in the checkout's files that Git does
    # not ignore comes back (remote.brought_back). --here, its paths and devices, nested and foreground work, and a
    # self lane's parts keep it here (guides/shared-host.md, Which Mac runs a lane).
    # Without a leading --, argparse collected the command, and a later -- belongs to it.
    command = args.command + ([] if args.passed is None else ["--", *args.passed]) if args.command else args.passed
    if not command: raise ValueError("Name the command: eng admit [--exclusive | --model] [--here] -- COMMAND")
    if args.time_limit <= 0: raise ValueError("--time-limit needs a positive number of seconds")
    config = config if config is not None else load(root)
    kind = ["--exclusive"] if args.exclusive else ["--model"] if args.model else []
    # Only a limit of its own goes to the second Mac, whose toolkit may not take the option.
    if args.time_limit != processes.DEFAULT_LIMIT: kind += ["--time-limit", f"{args.time_limit:g}"]
    # Foreground work holds the foreground of the Mac it runs on: the second Mac's, which eng takes for it, or this
    # Mac's, which the session holds within the owner's window. eng remote admit --foreground sends it there.
    second = args.foreground and foreground.second_mac()
    if args.foreground and args.verify:
        raise ValueError("--verify never sends foreground work: send it to the second Mac with "
                         "eng remote admit --foreground -- COMMAND")
    if args.app and not args.foreground:
        raise ValueError("--app names the app under test of foreground work, whose crash-report alerts eng clears: "
                         "add --foreground")
    if args.foreground and not second: devices.require_foreground(os.environ.get(devices.OWNER))
    # Nested work runs inside the admission it inherits, here.
    inherited = bool(os.environ.get(resources.TOKEN))
    reason = here_because(args, inherited)
    offered = None
    if reason is None:
        # Whichever Mac has spare capacity for it, the second Mac first (remote.first_command, admitted_here).
        offered = remote.first_command(root, config, command, kind)
        if not isinstance(offered.status, str): return offered.status
        reason = offered.status
    # The policy goes to stderr so that stdout carries only the command's own output.
    receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
               "policy": refresh_policy(root, sys.stderr), "command": command,
               "directory": str(Path.cwd()), "exclusive": args.exclusive, "model": args.model,
               "foreground": args.foreground, "timeLimit": args.time_limit, **placement(reason)}
    with processes.forwarding_signals(), contextlib.ExitStack() as stack:
        asked = time.monotonic()
        waiting = window_wait("the command", device_window, " ".join(command))
        allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
        closed = window_closed("the command", waiting)
        if closed is not None: return closed
        # The second Mac's foreground first, so that a command waiting for it holds no slot.
        waited = foreground_held(stack, "the command", " ".join(command), allowed(args.timeout)) if second else None
        if waited is not None: return not_admitted("the command", waiting, waited)
        closed = window_closed("the command", waiting)
        if closed is not None: return closed
        admission = admitted_here(stack, "the command", " ".join(command), allowed(args.timeout), offered, args.if_free,
                                  args.exclusive, args.model, placement=admission_placement(offered, reason))
        if isinstance(admission, int): return not_admitted("the command", waiting, admission)
        sent = offered.sent if offered is not None else {}
        # Once the tree here is no longer what was sent, the command runs from what was sent (#276).
        moved = remote.moved_trees(root, offered)
        tree = from_sent(stack, root, sent, moved, receipt) if moved else None
        if isinstance(tree, int): return tree
        if tree is None:
            receipt["project"] = source_identity(root)
            if sent.get("commit"): receipt["sent"] = sent["commit"]
        receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
        if device_window is not None: devices.report_window(device_window, receipt["queueSeconds"])
        if offered is not None:
            receipt["hereBecause"] = offered.status
            fell_back("the command", offered)
        reap_here(root, inherited)
        def work():
            # Finalize the alert report before record() writes the receipt, while admission still holds.
            with contextlib.ExitStack() as alert_stack:
                alerts = guarded_alerts(alert_stack, " ".join(command), receipt, args.app or ()) \
                    if args.foreground else None
                if isinstance(alerts, int): return alerts
                # The second Mac's Notification Center windows, under the foreground eng holds there (#204).
                blocked = cleared_notifications(receipt) if second else None
                if blocked is not None: return blocked
                github_artifacts.configured(root, config)
                directory, variables = Path.cwd(), None
                if tree is not None:
                    # In the worktree's directory that matches the current one, as on the second Mac, after its
                    # provision lane, with the toolkit beside it (remote.sent_tree).
                    checkout, toolkit = tree
                    directory = checkout / matching_directory(root)
                    variables = {**os.environ, "VOCARO_ENGINEERING_ROOT": str(toolkit)}
                    provisioned = provision_sent(root, config, checkout, toolkit, variables, args.time_limit,
                                                 admission.interrupted)
                    if provisioned: return provisioned
                # A foreground command restores what it changed on the Mac before SIGKILL, VoiceOver first.
                with app_registrations.around_run([command], directory):
                    status = processes.supervise(command, cwd=directory, env=variables, on_stop=admission.interrupted,
                                                 timeout=args.time_limit,
                                                 kill_after=processes.FOREGROUND_KILL_AFTER if args.foreground
                                                 else processes.KILL_AFTER)
                    status = alerts.judged(status) if alerts is not None else status
                # What it changed in the worktree's files that Git does not ignore, applied here as from the second Mac.
                if tree is not None: status = remote.brought_back_here(root, sent["commit"], tree[0], status)
                return status
        try:
            return record(root / ".build/engineering/admit", receipt, work)
        except processes.TimedOut as error:
            # A backstop for a hang that a busy host can also reach, so the command is unmeasured.
            print(f"eng: unmeasured: {error}", file=sys.stderr)
            return evidence.UNMEASURED


def main(argv=None):
    report = lane_report.Report()
    try:
        status = perform(argv, report)
    except Exception as error:
        report.finish(None, error)
        raise
    report.finish(status)
    return status


def perform(argv, report):
    """Run the command argv names, telling report of a lane run's progress for the summary that ends it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init")
    init.add_argument("--name")
    init.add_argument("--capability", action="append", choices=sorted(CAPABILITIES), default=[])
    commands.add_parser("doctor")
    local_setup = commands.add_parser("setup", help="check or install Claude Code issue permissions on this Mac")
    local_setup.add_argument("--check", action="store_true", help="report missing rules without changing settings")
    commands.add_parser("instructions")
    artifacts = commands.add_parser("github-artifacts", help="prepare checksum-pinned GitHub binary artifacts "
        "through authenticated API downloads in SwiftPM's artifact cache")
    artifacts.add_argument("--package", help="read this dependency's manifest at its Package.resolved commit")
    artifacts.add_argument("--cache-path", type=Path, help="SwiftPM or Xcode package cache root; default is the "
                           "user's standard SwiftPM cache")
    policy = commands.add_parser("policy")
    policy.add_argument("operation", choices=["refresh"])
    run = commands.add_parser("run", usage="%(prog)s [-h] [--expect N] [--repeat N [--erase|--template] [--only TEST]] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane, admitting a heavy or exclusive one, on whichever Mac has spare capacity for "
        "it now, the second Mac first, and on this Mac while the second Mac has none or cannot be reached, unless "
        "the lane says why it stays on this Mac: eng run LANE [-- ARGUMENTS], which appends the arguments, such as "
        "a test filter, to the lane's command")
    run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests: N selected with "
        "some skipped or canceled is unmeasured, exit 3, and any other count fails; without it, a run with arguments needs at least one in place of the lane's minimumTests")
    where = run.add_mutually_exclusive_group()
    where.add_argument("--here", action="store_true", help="run the lane on this Mac rather than on the second Mac: "
        "for a build whose products the session uses here, or, with the owner's decision, while the second Mac is "
        f"misconfigured (exit {remote.MISCONFIGURED})")
    where.add_argument("--offload-only", action="store_true", help="offer the lane to the second Mac, which runs it "
        f"only if it can start it now, and never run it here: exit {BUSY} when the second Mac declines it, cannot be "
        f"reached or none is configured, {remote.MISCONFIGURED} when it is misconfigured, else the lane's status "
        "there; for a caller that holds or will take its own admission here")
    # Its name until vocaro-engineering#58, which main refuses, naming the new one, beside --here too.
    run.add_argument("--build-host-only", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--if-free", action="store_true", help=f"run only if the second Mac's heavy-work admission, or "
        f"else this Mac's, admits the lane within {FREE_WAIT}s; otherwise exit {BUSY} without running it")
    run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on this "
        "Mac: the reference a failure seen only side by side is checked against")
    run.add_argument("--unlocked-screen", action="store_true", help="place the lane only on a Mac whose screen reads "
        "unlocked, and wait for one, as \"unlockedScreen\": true does: the merge queue's next run of a lane whose "
        "run a locked screen left without a verdict (guides/maintenance.md, A run with no verdict)")
    run.add_argument("--repeat", type=int, metavar="N", help="run a lane of eng simulator-tests N times, on one build, "
        "each run on a member taken from the pool afresh, and report each run's outcome, test counts and one-minute "
        "load (guides/simulator-testing.md, Repeating a test to show a fix works). Its receipt is not a pass of the "
        "whole lane")
    reset = run.add_mutually_exclusive_group()
    reset.add_argument("--erase", action="store_true", help="with --repeat: erase the member before each run")
    reset.add_argument("--template", action="store_true", help="with --repeat: copy the member's kind's warmed "
        "template before each run; a run the pool could only erase is unmeasured")
    run.add_argument("--only", action="append", default=[], metavar="TEST", help="with --repeat: run only this test, "
        "in place of the lane's own selection; repeat for each")
    run.add_argument("lane")
    # Listed for help only: main passes everything after these to their own parsers.
    commands.add_parser("simulator-tests", help="build a scheme's tests once and run them on several members of the "
        "project's simulator pool side by side: eng simulator-tests --scheme S --runtime R --device T --device T; "
        "-h for the rest")
    commands.add_parser("device-pass", help="a release's physical-device pass, as a release lane's command: run "
        "COMMAND once on each paired device named, in turn, under its reservation, taking the iPad in turn and "
        "using an iPhone the session reserved within the owner's window: eng device-pass --device iPad --device "
        "iPhone -- COMMAND; -h for the rest (guides/releasing.md, The device pass)")
    app_builds.add_arguments(commands.add_parser("app", help="run the last good build of the project's app: eng app "
        "run opens the newest kept Mac build, and eng app run --iphone or --ipad installs and launches the device "
        "build on that paired device, building origin/main first when the kept build is older; eng app keep builds "
        "and keeps one, as the merge queue does after each gated landing; eng app status lists them "
        "(guides/app-builds.md)"))
    commands.add_parser("simulator-pool", help="list, take, release, prune, reap or check members of the project's "
        "simulator pool under device-record reservations: eng simulator-pool list|take|release|prune|reap|check; "
        "-h for the rest")
    commands.add_parser("failures", help="keep a failed test run's log, result bundle and failure messages in a "
        "record under .build/failures/ that no later run replaces, with a copy in the lane's results/, and list or "
        "prune the records by their rule: eng failures keep|list|prune; -h for the rest")
    commands.add_parser("results", help="keep a test run's per-test outcomes and seconds in the lane's results/ and "
        "print them, for a script that runs xcodebuild itself: eng results keep; -h for the rest")
    commands.add_parser("unittest", help="run a unittest suite's modules in processes of their own, side by side: "
        "eng unittest [-s START] [-p PATTERN] [-j JOBS] [-k PATTERN] [-v] [NAME ...]; eng unittest -h for the rest")
    commands.add_parser("xctest", help="build a Swift package's tests once and run its XCTest classes in xctest "
        "processes of their own, side by side, each reporting its skips: eng xctest [-j JOBS] [--filter REGEX]; "
        "eng xctest -h for the rest")
    admission =commands.add_parser("admit", help="run any other heavy command (a build, swift test, xcodebuild "
        "test, a simulator run, a gate suite) through heavy-work admission on whichever Mac has spare capacity for "
        "it, the second Mac first, as eng run places a lane: eng admit -- COMMAND; what it changes in the "
        "checkout's files that Git does not ignore comes back and is applied here, while its build products stay "
        "on the Mac that ran it. Add --here for a command whose products this session uses here afterwards, "
        "--model for a generative-model run, and --exclusive only for work that needs the host to itself")
    kind = admission.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot, and the model with them, for the "
                      "timed run alone, built and set up beforehand as heavy work: a whole-machine or timing "
                      "measurement, or a model run that is timed or compared with a baseline")
    kind.add_argument("--model", action="store_true", help="hold one slot and the host's model lock, which one "
                      "model run holds at a time: on-device generation, on this Mac or in a simulator, another local "
                      "generative model on the GPU, or Core ML whose compute units include the GPU")
    admission.add_argument("--foreground", action="store_true", help="the command takes the screen, keyboard, "
        "pointer, focus or speech, as Mac VoiceOver and Mac UI tests do: on the second Mac eng holds its foreground for "
        "the command, waiting while another run holds it; on this Mac the session must hold mac-foreground within "
        "the owner's window (guides/shared-host.md, Attended Mac sessions)")
    admission.add_argument("--app", action="append", metavar="NAME", help="with --foreground, the app under test, "
        "as a crash-report alert names its process; repeat for each. A failed run during which eng cleared such an "
        "alert for another process, one that stood longer than one look, is unmeasured; one for NAME stays failed "
        "(guides/second-mac-foreground.md, Crash-report alerts)")
    placed = admission.add_mutually_exclusive_group()
    placed.add_argument("--here", action="store_true", help="run the command on this Mac, never offering it to the "
        "second Mac: for a command whose products this session uses here afterwards, such as build output, derived "
        "data, files Git ignores or an app it then launches. Without it, the command goes to the second Mac first, "
        "to run in its checkout's directory that matches this one, at HEAD or a snapshot commit, and stays here when "
        "its directory is outside the checkout or one Git ignores, it names this checkout's path, a simulator or "
        "device, it is nested in an admission or a self lane's part, or the second Mac has no spare capacity or "
        f"cannot be reached. What it changed there in files Git does not ignore is applied here; exit {remote.UNAPPLIED} "
        "says it could not be, and names the patch kept")
    # The default since the owner's decision of 2026-11-07, still accepted from callers that pass it.
    placed.add_argument("--verify", action="store_true", help="what eng admit does without --here: offer the "
        "command to the second Mac first")
    admission.add_argument("--if-free", action="store_true", help=f"run only if heavy-work admission admits the "
        f"command within {FREE_WAIT}s, on the second Mac or here; otherwise exit {BUSY} without running it")
    admission.add_argument("--timeout", type=float, default=5400, help="seconds to wait for admission (default 5400)")
    admission.add_argument("--time-limit", type=float, default=processes.DEFAULT_LIMIT, metavar="SECONDS",
        help=f"seconds the command may run once admitted before it and its processes are stopped and it exits "
        f"{evidence.UNMEASURED}, unmeasured (default {processes.DEFAULT_LIMIT}); a backstop for a hang")
    admission.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --; "
        "it runs in the current directory")
    logged = commands.add_parser("admissions", usage="%(prog)s [-h] [--since WHEN] [--until WHEN] [log ...]\n"
        "       %(prog)s recover [OWNER]\n       %(prog)s remove OWNER --why TEXT\n       %(prog)s limits\n"
        "       %(prog)s usage [--since WHEN] [--until WHEN] [--there LOG ...]", help="summarize this host's heavy-work admission "
        "log: for each kind and label, the requests admitted and withdrawn, how long they waited and held, and how "
        "admissions ended; eng admissions recover lists the guard's dead or uncertain entries, and eng admissions "
        "recover OWNER removes that owner's once a census against its process record finds nothing it ran still "
        "running; eng admissions remove OWNER --why TEXT removes by hand one recovery cannot prove finished once "
        "nothing its owner may have started is found running; eng admissions limits prints the admission limits in force and where each comes from; eng "
        "admissions usage shows both Macs' capacity now, the share of the last 3 hours each spent at each count of "
        "heavy jobs, and why this Mac's work ran here, never offered to the second Mac or declined by it",
        description="Summarize the admission log, or, with recover, list the guard's dead or uncertain entries and "
        "recover one: its owner, named by PID or identity, has ended, and nothing of the commands its process "
        "record leaves unsettled still runs. A live owner's entry, an entry without a process record and a "
        "guard that is unreadable or not in admission's format are left for inspection (guides/shared-host.md, "
        "Lock reclamation).")
    logged.add_argument("--since", type=resources.local_time, metavar="WHEN", help="count requests made from WHEN, "
                        "an ISO 8601 date or time, local unless it names a zone")
    logged.add_argument("--until", type=resources.local_time, metavar="WHEN", help="count requests made before WHEN")
    logged.add_argument("logs", nargs="*", type=Path, metavar="log", help="a copy of a log, such as the second Mac's, "
                        "to read instead of this host's; name the previous generation first")
    distant = commands.add_parser("remote", help="run a lane or other heavy command on the owner's second Mac over "
        "SSH: eng remote run LANE [-- ARGUMENTS] or eng remote admit [--foreground] -- COMMAND; eng remote passed "
        "LANE says whether the whole lane passed there at a commit; eng remote foreground says whether its "
        "foreground is ready")
    operations = distant.add_subparsers(dest="operation", required=True)
    remote_run = operations.add_parser("run", usage="%(prog)s [-h] [--host HOST] [--serial] [--expect N] "
        "[--device KEY] [--device-owner SESSION] [--checkout-slot N] [--checkout-wait SECONDS] "
        "lane [-- ARGUMENTS ...]",
        help="run a configured lane on the second Mac at HEAD, or at a snapshot commit of uncommitted work")
    remote_admit = operations.add_parser("admit", help="run a heavy command on the second Mac, in its checkout's "
        "root, at HEAD or at a snapshot commit of uncommitted work")
    remote_setup = operations.add_parser("setup", help="install on the second Mac, with Homebrew, the tools "
        f"every project's gates use ({', '.join(remote.SETUP_FORMULAE)}) and the formulae named, then check them as "
        "eng remote sees the second Mac; Homebrew itself the owner installs")
    remote_setup.add_argument("formulae", nargs="*", metavar="formula", help="a further Homebrew formula to install")
    remote_setup.add_argument("--import", dest="imports", action="append", metavar="MODULE",
                              help="check that the second Mac's python3 imports MODULE (repeatable)")
    remote_setup.add_argument("--check", action="store_true", help="only check; install or copy nothing")
    remote_foreground = operations.add_parser("foreground", help="say what the second Mac lacks for foreground work, "
        "Mac VoiceOver and Mac UI tests, started over SSH, each with the owner's fix, and which run holds its "
        f"foreground; exit 0 when it lacks nothing, {remote.MISCONFIGURED} otherwise")
    remote_setup.add_argument("--runtimes", action="store_true", help="instead, give this Mac and the second Mac the "
        "same simulator runtimes of the Xcode at this Mac's developer directory, as eng doctor does: a runtime only "
        "one Mac has is deleted there when no simulator but Xcode's defaults uses it, and otherwise its disk image "
        "is copied over SSH to the Mac that lacks it; it exits 0 only when both list the same")
    for choice in (remote_run, remote_admit, remote_setup, remote_foreground):
        choice.add_argument("--host", help=f"the second Mac's SSH destination (default: {remote.HOST_VARIABLE}, "
                            f"else the ~/.ssh/config alias {' or '.join(remote.ALIASES)} that is defined, else "
                            f"{remote.DEFAULT_HOST})")
    for choice in (remote_run, remote_admit):
        choice.add_argument("--device", action="append", metavar="KEY", help="a physical device the run uses, "
                            "reserved in this Mac's device record and held, with no run active, by the session "
                            "(repeatable); the dispatch records the run in its entry and "
                            "stops it before the reservation ends")
        choice.add_argument("--device-owner", metavar="SESSION", help="the session holding --device (default: "
                            f"{devices.OWNER}, else the agent session)")
        choice.add_argument("--checkout-slot", metavar="N", help="run in this one of the second Mac's checkouts of the "
                            f"project, 0 to {remote.CHECKOUTS - 1}, waiting for it under the checkout's lock, however "
                            "many others are free, to repeat a check in a named checkout (guides/shared-host.md, A "
                            "named checkout slot); by default the free checkout nearest the commit")
        choice.add_argument("--checkout-wait", type=float, metavar="SECONDS", help="with --checkout-slot, how long to "
                            f"wait for it (default {offload_host.CLAIM_TIMEOUT}); then exit {BUSY} without running")
    remote_run.add_argument("--expect", type=int, metavar="N", help="require exactly N executed tests")
    remote_run.add_argument("--serial", action="store_true", help="run a lane group's lanes one after another, on the "
        "second Mac under its admission: the serial reference of a group that runs there, which counts for no commit")
    remote_run.add_argument("lane")
    kind = remote_admit.add_mutually_exclusive_group()
    kind.add_argument("--exclusive", action="store_true", help="hold every slot of the second Mac")
    kind.add_argument("--model", action="store_true", help="hold one slot and the model lock of the second Mac")
    remote_admit.add_argument("--foreground", action="store_true", help="hold the second Mac's foreground for the "
        "command, as Mac VoiceOver and Mac UI tests need, waiting while another run holds it")
    remote_admit.add_argument("--app", action="append", metavar="NAME", help="with --foreground, the app under "
        "test, as eng admit's")
    remote_admit.add_argument("--time-limit", type=float, metavar="SECONDS", help="seconds the command may run "
        f"there once admitted, as eng admit's (default {processes.DEFAULT_LIMIT})")
    remote_admit.add_argument("command", nargs=argparse.REMAINDER, help="the command and its arguments, after --")
    remote_passed = operations.add_parser("passed", help="exit 0 when the whole lane passed on the second Mac at "
        "exactly this commit; a snapshot's run never counts")
    remote_passed.add_argument("lane")
    remote_passed.add_argument("--commit", default="HEAD")
    remote_prune = operations.add_parser("prune-results", help="remove the lane results kept by commit beside the "
        f"receipts in the repository's Git directory that the rule reclaims, which {remote.KEPT_RESULTS_RULE}; "
        "eng disk lists them")
    remote_prune.add_argument("checkout", nargs="?", type=Path, help="a checkout of the repository (default: the project's)")
    remote_prune.add_argument("--dry-run", action="store_true", help="list them; remove nothing")
    merge_queue.add_arguments(commands.add_parser("queue", help="land finished commits through the repository's one "
        "merge queue, which gates batches on origin/main and pushes them; a session adds its commit and moves on "
        "(guides/maintenance.md, The merge queue)"))
    merge_queue.land_arguments(commands.add_parser("land", help="land a finished commit through the merge queue of this "
        "repository, or with --repo of another, and return at once: the queue gates it on origin/main and pushes it"))
    publication = commands.add_parser("release", help="tag a library's release and publish it on GitHub once its "
        "gate and each lane in engineering.json's release.lanes passed at the commit and origin/main holds it: "
        "eng release X.Y.Z COMMIT --notes NOTES.md; each step "
        "stops the rest when it fails, and a run that stopped part way can run again (guides/releasing.md)")
    publication.add_argument("version", metavar="X.Y.Z")
    publication.add_argument("commit", metavar="COMMIT")
    publication.add_argument("--notes", required=True, type=Path, help="the release notes, in a file in the checkout "
                             "that Git does not ignore, which eng spelling must pass")
    publication.add_argument("--waive", action="append", metavar="LANE=ISSUE", help="the owner's waiver of a release "
                             "lane's failure, filed in ISSUE, which the tag's message records; only the owner passes it")
    checkpointing = commands.add_parser("checkpoint", help="run each lane in engineering.json's release.lanes one "
        "after another on main's tip, a device pass first, each through eng run's admission and placement, and say "
        "which passed, failed, were unmeasured or did not run; exit 0 only when every one passed (guides/releasing.md, "
        "Checkpoints)")
    checkpointing.add_argument("--leave-out", action="append", metavar="LANE=WHY", help="leave a release lane out of "
        "this run, reported as not run with the reason, such as a device lane the owner has not agreed to")
    evid = commands.add_parser("evidence", help="verify an evidence receipt, or move a durable evidence folder "
                               "to the second Mac, deleting it here once every file verified there (guides/evidence.md)")
    evid.add_argument("operation", choices=["verify", "offload"])
    evid.add_argument("directory", type=Path, nargs="?", help="offload: with no folder, offer every folder that an "
                      "offload found the second Mac unreachable for to it again")
    evid.add_argument("--to", help="offload: the path on the second Mac, relative to its home directory "
                      "(default: the folder's path relative to this Mac's home)")
    evid.add_argument("--keep", action="store_true", help="offload: keep the folder here after it verified there")
    documentation = commands.add_parser("docs")
    documentation.add_argument("operation", choices=["check"])
    documentation.add_argument("paths", nargs="*")
    commands.add_parser("storekit", help="check StoreKit configuration files against App Store Connect's limits "
        "before a product is created (check)")
    spell = commands.add_parser("spelling", help="find British spellings in the project's text, identifiers "
        "included, and file names; exit 1 on any, 2 when nothing was checked")
    spell.add_argument("paths", nargs="*", help="files or directories; untracked files under them are read too")
    knowledge = commands.add_parser("knowledge")
    knowledge.add_argument("query", nargs="?", default="")
    source = commands.add_parser("source")
    source.add_argument("name", choices=["voiceover-ax-observer.swift", "voiceover-link-verbosity.swift",
                                        "background-mac-probe.swift", "probe-simulator-voiceover-activation.m",
                                        "duo-pose-helper.c"])
    disk.add_arguments(commands.add_parser("disk", help="inventory reclaimable disk space; changes nothing without --delete"))
    keychain.add_arguments(commands.add_parser("keychain", help="before the owner unlocks a keychain, say which Mac's "
        "keychain the work needs and print commands that run only there (recover); keep the second Mac's login "
        "keychain unlocked for every eng remote run (keep-unlocked); where the automation runs, check the keychain, "
        "the signing identity and signing with it, without a prompt (check)"))
    hosts = commands.add_parser("host", help="read a Mac's state as eng reads it")
    reads = hosts.add_subparsers(dest="operation", required=True)
    screen = reads.add_parser("screen", help="say whether a Mac's console screen is locked, from both keys the "
        "IORegistry holds and the console user, and exit 0 unlocked, 1 locked or 2 unknown")
    screen.add_argument("--remote", action="store_true", help="read the second Mac's screen over SSH")
    screen.add_argument("--host", help="the second Mac's SSH name, when not the configured one")
    screen.add_argument("--json", action="store_true", help="print the reading as JSON")
    notices = reads.add_parser("notifications", help="list the Notification Center windows over this Mac's screen, "
        "exiting 0 when none is and 3 when one is; with --dismiss, on the second Mac under the foreground lease the "
        "caller holds, close their notifications and confirm each window gone, as a foreground run does before it "
        "starts (exit 0, 3 naming a window left, 1 without a lease)")
    notices.add_argument("--dismiss", action="store_true", help="close the windows' notifications; needs the second "
                         "Mac's mac-foreground lease, named by VOCARO_DEVICE_OWNER")
    linking = commands.add_parser("skills", help="check that Claude Code and Codex load the toolkit's agent skills "
        "through links to its main checkout; changes nothing without --link")
    linking.add_argument("--link", action="store_true", help="link each installed host's missing skills, and those "
                         "that link elsewhere; a copy is never replaced")
    # Everything after the first -- belongs to the command. Python 3.9's argparse rejects options
    # before it and drops a later --, so argparse never sees it.
    argv = sys.argv[1:] if argv is None else list(argv)
    # unittest's options are its own runner's, which this parser would refuse. It reads no project.
    first = 2 if argv[:1] == ["--project"] else 1 if argv[:1] and argv[0].startswith("--project=") else 0
    if argv[first:first + 1] == ["unittest"]: return parallel.unittest_main(argv[first + 1:])
    if argv[first:first + 1] == ["xctest"]: return xctest_shards.main(argv[first + 1:])
    project = argv[1] if first == 2 else argv[0].split("=", 1)[1] if first == 1 else None
    if argv[first:first + 1] == ["simulator-tests"]: return matrix.main(argv[first + 1:], project)
    if argv[first:first + 1] == ["simulator-pool"]: return simulator_pool.main(argv[first + 1:], project)
    if argv[first:first + 1] == ["device-pass"]: return device_pass.main(argv[first + 1:], project)
    if argv[first:first + 1] == ["failures"]: return failure_records.main(argv[first + 1:], project)
    if argv[first:first + 1] == ["results"]: return run_results.main(argv[first + 1:])
    if argv[first:first + 1] == ["storekit"]: return storekit_config.main(argv[first + 1:], project)
    # Recovery, like the admission log, covers the whole host and reads no project.
    if argv[first:first + 2] == ["admissions", "recover"]: return resources.recover_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "remove"]: return resources.remove_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "limits"]: return resources.limits_main(argv[first + 2:])
    if argv[first:first + 2] == ["admissions", "usage"]: return resources.usage_main(argv[first + 2:])
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    if args.action == "setup":
        if split < len(argv):
            parser.error("setup takes no arguments after --")
        # This check must not recover crash-alert settings or start any host work.
        return setup.main(args.check)
    # Only the remote work itself reports its admission. Provisioning and nested commands do not.
    window = os.environ.pop(devices.WINDOW, None)
    device_window = json.loads(window) if window is not None else None
    args.passed = argv[split + 1:] if split < len(argv) else None
    if args.passed is not None and (args.action, getattr(args, "operation", None)) not in {
            ("run", None), ("admit", None), ("remote", "run"), ("remote", "admit")}:
        parser.error("only run and admit take arguments after --")
    if args.action == "evidence" and args.operation == "verify" and args.directory is None:
        parser.error("evidence verify needs a directory")
    # The commands that check the second Mac again on every run offer it the evidence that an offload
    # found it unreachable for, in the background (guides/evidence.md).
    if args.action in OFFLOAD_RETRIES: evidence_offload.retry_soon()
    root = args.project.resolve()
    # A foreground run whose eng died left crash-report dialogs off; the next eng command puts them back.
    crash_alerts.recover()
    try:
        if args.action == "init":
            initialize(root, args)
            return 0
        # The inventory, the skill links and the admission log cover the whole host, so they need no project
        # configuration.
        if args.action == "disk": return disk.execute(args)
        # A keychain's Mac and its session are the host's, not a project's.
        if args.action == "keychain": return keychain.execute(args)
        if args.action == "skills": return skills.main(named_checkout(TOOLKIT_ROOT), args.link)
        # A Mac's screen is the host's, not a project's.
        if args.action == "host" and args.operation == "notifications":
            return notification_center.main(["check" if args.dismiss else "list"])
        if args.action == "host": return host_screen(args)
        if args.action == "admissions": return resources.report(args.logs or None, args.since, args.until)
        # Offloaded evidence goes to the host's second Mac, whatever project holds it.
        if args.action == "evidence" and args.operation == "offload":
            if args.directory is None:
                if args.to or args.keep: raise ValueError("--to and --keep name a folder's offload; name the folder")
                return evidence_offload.retry()
            return evidence_offload.offload(args.directory, to=args.to, keep=args.keep)
        # The queue reads a project's settings from its commits, and never from this working tree,
        # which for its worker is a main checkout that may lag.
        if args.action == "land": return merge_queue.land(root, args, Path.cwd())
        if args.action == "queue": return merge_queue.main(root, args)
        # The store is the repository's, named by any checkout of it, and eng disk runs this for every project.
        if args.action == "remote" and args.operation == "prune-results":
            return remote.prune_results_main(args.checkout or root, args.dry_run)
        config = load(root)
        if args.action == "app": return app_builds.execute(root, args, config)
        if args.action == "instructions": instructions(config)
        elif args.action == "doctor":
            required = {"git", "python3"}
            if any(c.startswith("simulator") or c == "packaging" for c in config.get("capabilities", [])):
                required |= {"xcrun", "xcodebuild"}
            missing = sorted(name for name in required if not shutil.which(name))
            warnings = []
            if "integration" not in config:
                warnings.append("engineering.json declares no integration: add \"integration\": \"direct-main\", "
                                "or \"pull-request\" for a project that takes changes only by pull request (agents/core.md)")
            unbounded = sorted(name for name, lane in config.get("lanes", {}).items()
                               if "command" in lane and "timeout" not in lane)
            if unbounded:
                warnings.append(f"{len(unbounded)} lane(s) declare no timeout and are stopped, unmeasured, after "
                                f"eng's default {processes.DEFAULT_LIMIT}s: {', '.join(unbounded)}; give each a timeout "
                                "chosen from its measured time (guides/parallel-tests.md, Bound every run)")
            if config.get("lanes") and "gate" not in config["lanes"] and not config.get("queue", {}).get("lanes") \
                    and config.get("integration", "direct-main") == "direct-main":
                warnings.append("no lane is named gate, which eng land and eng release run: name the lane holding "
                                "what a commit must pass before it lands gate (guides/adoption.md, Lane names)")
            strays = [name for name in config.get("release", {}).get("lanes", []) if name not in config.get("lanes", {})]
            if strays:
                warnings.append(f"release.lanes names {', '.join(strays)}, which is no lane in lanes, so eng release "
                                "finds no pass of it and stops: add the lane or correct the name (guides/releasing.md, "
                                "Two gates)")
            warnings += budgets.doctor_warnings(config)
            warnings += self_lane_warnings(root, config)
            warnings += display_lane_warnings(config)
            warnings += split_warnings(root, config)
            warnings += temporary_model_storage(root, config)
            warnings += unlinked_packages(root, config)
            warnings += stale_copies(root)
            warnings += toolkit_copy.doctor_warnings(TOOLKIT_ROOT)
            warnings += agent_entry(root, config)
            from .waiting import doctor_warnings as fixed_wait_warnings  # #284
            warnings += fixed_wait_warnings(root)
            warnings += skills.warnings(named_checkout(TOOLKIT_ROOT))
            # A difference between the two Macs is an error; a second Mac that cannot be reached is unchecked.
            hosts = fix_hosts(root, remote.compare_hosts())
            warnings += default_simulators(config)
            test_lanes = lane_coverage.report(root, config)  # a warning at most, and never a failure (#289)
            warnings += lane_coverage.doctor_warnings(test_lanes)
            print(json.dumps({"project": config["name"], "toolkit": str(TOOLKIT_ROOT),
                "source": source_identity(TOOLKIT_ROOT), "capabilities": config.get("capabilities", []),
                "missingCommands": missing, "warnings": warnings, "testLanes": test_lanes, "offload": hosts}, indent=2))
            return int(bool(missing or hosts["errors"]))
        elif args.action == "github-artifacts": github_artifacts.prepare(root, args.package, args.cache_path)
        elif args.action == "policy": refresh_policy(root)
        elif args.action == "admit": return admit(root, args, config, device_window=device_window)
        elif args.action == "remote": return remote.main(root, config, args)
        elif args.action == "release": return release.main(root, config, args)
        elif args.action == "checkpoint": return checkpoint.main(root, config, args)
        elif args.action == "source":
            from .support import source_path
            print(source_path(args.name))
        elif args.action == "docs":
            failures = docs.check(root, args.paths or None)
            if not args.paths or {"AGENTS.md", "CLAUDE.md"} & {Path(path).name for path in args.paths}:
                failures += agent_entry(root, config)
            # The speech predictor's rules cite knowledge entries; a project without the rules has none to check.
            if not args.paths or any(Path(path).suffix == ".json" or "knowledge" in Path(path).parts for path in args.paths):
                from .voiceover import speech
                failures += speech.check_rules(root)
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
            report.begin(root, args.lane)
            if args.build_host_only:
                raise ValueError("--build-host-only was --offload-only's name until vocaro-engineering#58, and is no "
                                 "longer read: use --offload-only (guides/shared-host.md, Earlier names)")
            lane = lane_to_run(config, args.lane)
            if lane is None: raise ValueError(f"No configured lane: {args.lane}")
            if args.expect is not None and args.expect < 1: raise ValueError("--expect needs at least 1 test")
            if args.unlocked_screen:
                # Placed from here on as a lane that says "unlockedScreen": true is, on whichever Mac runs it
                # (vocaro-engineering#293). A self lane runs where it starts, and its script places its parts.
                if unadmitted(lane):
                    print(f"eng: {args.lane} runs where it starts, as a self lane, so --unlocked-screen does not place "
                          "it", file=sys.stderr, flush=True)
                else: lane["unlockedScreen"] = True
            if args.repeat is not None and args.repeat < 1:
                raise ValueError("--repeat needs at least 1 run: a series of none shows nothing")
            if args.repeat is None and (args.erase or args.template or args.only):
                raise ValueError("--erase, --template and --only say how a series runs: give --repeat N too")
            series = [] if args.repeat is None else ["--repeat", str(args.repeat), *(["--erase"] if args.erase else []),
                                                     *(["--template"] if args.template else []),
                                                     *[word for test in args.only for word in ("--only", test)]]
            # Lane arguments, so that eng simulator-tests finds them on whichever Mac runs the lane.
            extra = [*series, *(args.passed or [])]
            # A series' lane runs N times inside its one admission, within N times its limit (guides/simulator-testing.md).
            limit = parallel.lane_timeout(lane, extra)
            lane_wait.begin(root, args.lane)
            # A foreground lane takes a Mac's screen. The second Mac's is eng's to take for the run, below; this
            # Mac's, only the session holding mac-foreground within the owner's window runs it on, with --here.
            # Otherwise it goes to the second Mac and waits there for its foreground, never falling back to this
            # Mac (foreground.py).
            second = lane.get("foreground") and foreground.second_mac()
            if lane.get("foreground") and not second:
                if args.offload_only or not (args.here or lane.get("here")):
                    return remote.foreground_lane(root, config, args.lane, extra, args.expect)
                devices.require_foreground(os.environ.get(devices.OWNER))
            group = report.group = lane.get("parallel")
            if group is not None and (extra or args.expect):
                raise ValueError(f"{args.lane} runs a group of lanes, which takes no arguments or --expect; "
                                 "pass them to one of its lanes")
            if group is None and args.serial: raise ValueError(f"--serial runs a lane group, and {args.lane} is not one")
            # Why the lane runs only here, as its configuration says, or None: it goes to the second Mac.
            reason = stays(config["lanes"], args.lane)
            if args.offload_only:
                if reason is not None:
                    raise ValueError(f"{args.lane} stays on this Mac ({reason}), so --offload-only cannot offer it "
                                     "to the second Mac")
                if args.serial: raise ValueError("--serial runs a lane group here, and --offload-only never does")
                # The caller runs the lane under its own admission when the second Mac declines it.
                return remote.first(root, config, args.lane, extra, args.expect, fallback=False).status
            # Only a queue worker's own `eng run` of a gate's lane stops for lost Local Network access, and not what
            # that lane starts here (vocaro-engineering#300).
            queue_gate = bool(os.environ.pop(remote.QUEUE_GATE, None))
            # Nested work runs inside the admission it inherits, here. The serial reference runs here too.
            inherited = bool(os.environ.get(resources.TOKEN))
            reason = reason or ("--here" if args.here else "--serial runs the serial reference here" if args.serial
                                else "nested in the admission it inherits" if inherited else None)
            offered = None
            if reason is None:
                # Whichever Mac has spare capacity for the lane, the second Mac first, and while neither has,
                # whichever admits the lane first (remote.first, admitted_here).
                offered = remote.first(root, config, args.lane, extra, args.expect, queue_gate=queue_gate)
                if not isinstance(offered.status, str): return offered.status
                reason = offered.status
            # Every lane is admitted, a light one as heavy work, except a self lane, whose script admits its parts.
            resource = admission_of(lane)
            # A lane whose result depends on the screen has it read on the Mac that runs it, at its start and again
            # when it ends unmeasured (vocaro-engineering#185). One that needs it unlocked waits for that, unless it
            # is nested in an admission that placed it already.
            needs = unlocked_screen(config["lanes"], args.lane)
            reads_screen = needs or any(config["lanes"][name].get("display") for name in (group or [args.lane]))
            policy_commit = refresh_policy(root) if resource != "self" else None
            directory = root / ".build/engineering" / args.lane
            receipt = {"project": source_identity(root), "toolkit": source_identity(TOOLKIT_ROOT),
                       "policy": policy_commit, **placement(reason)}
            # A run of the whole lane, which a pass of counts for its commit once kept (concluded).
            whole = not extra and args.expect is None and not args.serial
            if whole and receipt["project"].get("workingChangesSHA256") != remote.CLEAN:
                # vocaro-engineering#197: nothing said so until eng release stopped for it.
                why = remote.uncounted(root, args.lane)
                if why: print(f"eng: {why}", file=sys.stderr, flush=True)
            def concluded(status, **fields):
                # The verdict in this checkout, and kept by commit for every worktree (vocaro-engineering#198).
                conclude(directory, receipt, status, **fields)
                remote.keep_here(root, args.lane, receipt, status, whole, results=directory / "results")
                # A lane's output goes to its log; the Mac that sent a dispatch prints these once they are back.
                if not os.environ.get(remote.DISPATCHER): run_results.report(directory / "results")
            def prepared(work):
                # What an earlier run left in results/ is not this run's, here as on the second Mac (remote.RESULTS).
                # The lane finds the directory in ENG_RESULTS_DIR, and a group's lanes each find one of their own in it.
                shutil.rmtree(directory / "results", ignore_errors=True)
                report.ran_here(directory)
                if resource != "self": github_artifacts.configured(root, config)
                commands = [config["lanes"][name]["command"] for name in group] if group is not None else \
                    [lane["command"], *([lane["compileFirst"]] if "compileFirst" in lane else [])]
                try:
                    with app_registrations.around_run(commands, root): return work()
                finally:
                    # Nested in another lane, its results go into that lane's too, however it ended (#218).
                    parallel.hand_up(directory / "results", args.lane)
            if group is None:
                command = awake(lane, extra)
                receipt.update(command=command, arguments=extra, timeout=limit)
                def execute(on_stop=None):
                    def work():
                        env = stale_modules.environment(parallel.results_environment(directory / "results"), root)
                        kill_after = processes.FOREGROUND_KILL_AFTER if lane.get("foreground") else processes.KILL_AFTER
                        failed, left = compile_first(lane, root, directory, receipt, limit,
                                                     env, on_stop, kill_after)
                        if failed is not None: return failed
                        return processes.run(command, cwd=root, log_path=directory / "run.log", timeout=left,
                                             on_stop=on_stop, env=env, kill_after=kill_after)
                    return record(directory, receipt, lambda: prepared(work))
            else:
                jobs = 1 if args.serial else lane.get("jobs", len(group))
                receipt.update(parallel=group, jobs=jobs)
                # Each lane's receipt shows the command it ran, under the display's assertions or not.
                members = [(name, {**config["lanes"][name], "command": awake(config["lanes"][name])}) for name in group]
                def execute(on_stop=None):
                    return record(directory, receipt, lambda: prepared(lambda: parallel.lanes(root, members, directory / "lanes", jobs,
                                  lambda member, status, log: judge(member, status, log)[0], receipt, on_stop,
                                  results_directory=directory / "results")))
            try:
                with processes.forwarding_signals():
                    if resource == "self":
                        if device_window is not None: devices.report_window(device_window, 0)
                        scroll_bars_recorded(receipt, args.lane, inherited)
                        # Each part its script admits stays on this Mac, which holds what an earlier part built.
                        lane_wait.running()
                        with patch_environment({SELF_LANE: args.lane}):
                            result = execute()
                    else:
                        with contextlib.ExitStack() as stack:
                            asked = time.monotonic()
                            # The window the session holds the lane's devices for, here or leased by the Mac that
                            # sent it. Arguments such as a filter make a run no earlier one measures.
                            device_window = device_window if device_window is not None else devices.held_window(
                                lane_devices(lane), devices.default_owner())
                            waiting = window_wait(args.lane, device_window,
                                                  None if extra else lane_label(root, config, args.lane))
                            allowed = waiting.seconds if waiting is not None else lambda seconds: seconds
                            closed = window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            # The second Mac's foreground first, so that a run waiting for it holds no slot.
                            waited = foreground_held(stack, args.lane, lane_label(root, config, args.lane),
                                                     allowed(remote.OVERFLOW_TIMEOUT),
                                                     expected=lane.get("timeout", foreground.EXPECTED)) if second else None
                            if waited is not None: return not_admitted(args.lane, waiting, waited)
                            closed = window_closed(args.lane, waiting)
                            if closed is not None: return closed
                            admission = admitted_here(stack, args.lane, lane_label(root, config, args.lane),
                                                      allowed(remote.OVERFLOW_TIMEOUT), offered, args.if_free,
                                                      resource == "exclusive", lane.get("model", False),
                                                      lane.get("wholeHost", False),
                                                      needs and not os.environ.get(resources.TOKEN),
                                                      admission_placement(offered, reason))
                            if isinstance(admission, int): return not_admitted(args.lane, waiting, admission)
                            stopped = admitted_tree(root, args.lane, offered, receipt)
                            if stopped is not None: return stopped
                            # The wait in the queue, apart from what came before it (remote.execute).
                            receipt["queueSeconds"] = round(time.monotonic() - asked, 3)
                            if device_window is not None: devices.report_window(device_window, receipt["queueSeconds"])
                            if offered is not None:
                                receipt["hereBecause"] = offered.status
                                fell_back(args.lane, offered)
                            reap_here(root, inherited)
                            if reads_screen: receipt["screen"] = screen_reading()
                            scroll_bars_recorded(receipt, args.lane, inherited)
                            alerts = guarded_alerts(stack, args.lane, receipt, lane.get("apps", ())) \
                                if lane.get("foreground") else None
                            if isinstance(alerts, int): return alerts
                            blocked = cleared_notifications(receipt) if second else None
                            if blocked is not None: return blocked
                            result = execute(admission.interrupted)
                            # A failed build ran no test an alert could have disturbed.
                            if alerts is not None and not compile_failed(receipt): result = alerts.judged(result)
            except processes.TimedOut as error:
                # The lane's timeout is its backstop, so a run a busy host slowed past it is
                # unmeasured (guides/measurement.md). Its log is incomplete and counts nothing.
                print(f"eng: unmeasured: {error}", file=sys.stderr)
                report.explain(error, complete=False)
                return evidence.UNMEASURED
            # A group's lanes were each judged by their own logs.
            if "parallel" in lane:
                if reads_screen and result == evidence.UNMEASURED: unmeasured_screen(args.lane, receipt)
                concluded(result)
                return result
            # The command has a confirmed terminal result. Coverage validation cannot turn
            # that completed process into an orphan reservation.
            log = (directory / "run.log").read_text(errors="replace")
            try:
                status, unmeasured = judge(lane, result, log, extra, args.expect)
            except ValueError as error:
                concluded(2, error=str(error))
                raise
            if status == evidence.UNMEASURED and (reads_screen or SCREEN_LOCKED.search(log)):
                unmeasured_screen(args.lane, receipt)
            concluded(status, **({"unmeasured": unmeasured} if status == evidence.UNMEASURED else {}))
            return status
        return 0
    except (remote.Lost, PolicyUnavailable) as error:
        print(f"eng: {error}", file=sys.stderr)
        report.explain(error)
        return evidence.UNMEASURED
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"eng: {error}", file=sys.stderr)
        report.explain(error)
        return 2
    finally:
        lane_wait.finish()
