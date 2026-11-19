"""The last good build of a project's app, kept on this Mac and run from there: `qk app run|keep|status`.

A gate builds the app, but as a test build in the queue checkout's DerivedData, which the next batch replaces,
sometimes on another Mac, and never for a physical device. So the merge queue, once it has pushed a gated
batch, starts `qk app keep --after-landing` in the background (merge_queue.Worker.keep_app), which builds the
pushed commit as an ordinary build and keeps the product here. "Good" is a commit that landed through the gate
and then built: a build that fails keeps nothing, and the build kept before it stays the one that runs.

queuekeeper.json's `app` says how to build (config.app):

    "app": {"project": "Name.xcodeproj",        # or "workspace": "Name.xcworkspace"
            "directory": ".",                   # where the project is, inside the checkout
            "configuration": "Debug",
            "prepare": ["xcodegen", "generate"],  # run in directory before each build, when needed; or a
                                                  # list of such argv lists, run in order
            "mac": {"scheme": "Name", "product": "Name.app", "settings": {"NAME": "VALUE"},
                    "afterLanding": true},
            "ios": {"scheme": "Name", "afterLanding": false}}

Everything lives in the account's Application Support directory, found as the device record's is, so that every
session and the queue share one store, outside every checkout and outside Xcode's DerivedData:

    apps/NAME/source/          a detached worktree of the project, locked, where each build checks out its commit
    apps/NAME/vocaro-engineering   a link to the shared toolkit checkout, where toolkit.path finds it from source
    apps/NAME/derived/PLATFORM DerivedData for each platform, kept between builds so that each is incremental
    apps/NAME/kept/PLATFORM/SHORT/Name.app    the kept builds, the newest KEPT of each platform
    apps/NAME/kept.json        what each kept build is: its commit, bundle identifier, product and when it built
    apps/NAME/logs/            each build's log
    apps/NAME/lock             held by a keep while it builds, so that one build at a time uses source and derived

A build runs through `qk admit --here`, on this Mac, since its product is this Mac's. `qk app run` opens the newest kept Mac
build, or with --iphone or --ipad installs the newest kept device build on that paired device and launches it,
under the device's reservation as `qk device-pass` takes one: in turn, first come first served, or under a
reservation the session holds. It checks no approval: a session runs it on the owner's iPhone only when he has
agreed, which is between the session and the owner (owner's decision, 2026-11-06). Before
it runs one, it builds origin/main when the newest kept build is older, unless --no-build, and runs the newest
kept build, saying so, when that build fails. Opening an app on this Mac uses the owner's screen: it is the
owner's command, or a session's at his request.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import getpass
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import time

from queuekeeper_mobile import device_pass, devices
from . import apple_platform  # noqa: F401 -- switches the Apple platform on
from queuekeeper import processes
from .settings import APP_PLATFORMS
from queuekeeper.checkout import import_paths
from queuekeeper.config import TOOLKIT_ROOT, load

# Names an isolated store for tests; sessions use the default.
OVERRIDE = "QUEUEKEEPER_APP_STORE"
# The kept builds of each platform: the newest, and the one before it to go back to.
KEPT = 2
MAIN = "refs/remotes/origin/main"
FETCH = ["fetch", "--no-tags", "origin", f"refs/heads/main:{MAIN}"]
# What locks the store's source worktree, so that no sweep removes it (disk.worktree keeps a locked worktree).
SOURCE_REASON = "qk app's source checkout for kept app builds (app_builds.py)"
DESTINATIONS = {"mac": "generic/platform=macOS", "ios": "generic/platform=iOS"}
# The folder under Build/Products that holds each platform's product, after the configuration's name.
PRODUCT_SUFFIX = {"mac": "", "ios": "-iphoneos"}
# The device each --iphone or --ipad names, by its entry's key in the device record.
DEVICES = {"iphone": "iPhone", "ipad": "iPad"}
# Seconds a build may take once admitted, and a prepare step at all.
BUILD_LIMIT = 3600
PREPARE_LIMIT = 900
GIT_TIMEOUT = 300
# Seconds a device install and a launch may take: an install over Wi-Fi took more than two minutes under load,
# and the first launch of a run can fail inside devicectl before the app starts (apple-engineering's guides/device-testing.md,
# devicectl states and timeouts), so a launch is tried twice.
INSTALL_LIMIT = 600
LAUNCH_LIMIT = 120
# Minutes a device reservation for an install and launch lasts.
DEVICE_MINUTES = 20
# Seconds `qk app run` waits for a keep under way before it gives up on a fresh build.
LOCK_WAIT = 3 * 3600
# Seconds an app asked to quit has to end.
QUIT_WAIT = 30


class Failed(RuntimeError):
    """What `qk app` could not do, said to the person who ran it."""


def say(text):
    print(f"queuekeeper app: {text}", file=sys.stderr, flush=True)


def store_root():
    """The account's store of kept builds. A HOME private to one session must not start another."""
    if os.environ.get(OVERRIDE): return Path(os.environ[OVERRIDE])
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if sys.platform == "darwin": return home / "Library/Application Support/queuekeeper/apps"
    return home / ".local/state/queuekeeper/apps"


def git_environment():
    """The environment without a hook's GIT_DIR and the like, which would aim Git at another checkout."""
    names = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL).stdout.split()
    return {key: value for key, value in os.environ.items() if key not in set(names)}


def git(directory, *arguments, check=True, timeout=GIT_TIMEOUT):
    done = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=git_environment(), timeout=timeout)
    if check and done.returncode:
        raise Failed(f"git {' '.join(arguments)} in {directory} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done


def platforms(config):
    """The platforms the project's app builds for, in APP_PLATFORMS's order."""
    return [name for name in APP_PLATFORMS if name in config.get("app", {})]


def after_landing(config):
    """The platforms the queue keeps a build of after each gated landing."""
    app = config.get("app", {})
    return [name for name in platforms(config) if app[name].get("afterLanding", APP_PLATFORMS[name])]


class Store:
    """One project's kept builds and what builds them."""

    def __init__(self, name, root=None):
        self.path = Path(root or store_root()) / name
        self.source = self.path / "source"
        self.index = self.path / "kept.json"
        self.logs = self.path / "logs"

    def derived(self, platform):
        return self.path / "derived" / platform

    def read(self):
        try:
            value = json.loads(self.index.read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def write(self, value):
        self.path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path, delete=False, suffix=".json") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(handle.name, self.index)

    def kept(self, platform):
        """The kept builds of platform whose product is still there, newest first."""
        return [build for build in self.read().get(platform, []) if Path(build.get("app", "")).is_dir()]

    @contextlib.contextmanager
    def locked(self, wait=LOCK_WAIT, waiting=None):
        """Hold the store's lock, waiting up to wait seconds for a keep under way; TimeoutError past that."""
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.path / "lock", "a+") as handle:
            deadline, told = time.monotonic() + wait, False
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not told and waiting: waiting()
                    told = True
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"another keep held {self.path} for {wait:g}s")
                    time.sleep(2)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def busy(self):
        """Whether a keep holds the lock now."""
        path = self.path / "lock"
        if not path.exists(): return False
        with open(path, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False

    def record(self, platform, build):
        """Keep build as platform's newest and remove what falls past KEPT; the builds removed."""
        value = self.read()
        builds = [build] + [old for old in value.get(platform, []) if old.get("commit") != build["commit"]]
        value[platform], dropped = builds[:KEPT], builds[KEPT:]
        self.write(value)
        for old in dropped:
            folder = Path(old.get("app", "")).parent
            if folder.is_dir() and self.path in folder.parents: shutil.rmtree(folder, ignore_errors=True)
        return dropped


def repository(root):
    """The main checkout of root's repository, whose worktree list holds the store's source."""
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    return common.parent if common.name == ".git" else common


def ensure_source(root, store):
    """The store's source worktree, made from root's repository the first time and locked."""
    if (store.source / ".git").exists(): return store.source
    if store.source.exists():
        raise Failed(f"{store.source} exists but is no worktree; move it aside to let qk app make its own there")
    store.source.parent.mkdir(parents=True, exist_ok=True)
    git(repository(root), "worktree", "add", "--detach", "--lock", "--reason", SOURCE_REASON, str(store.source), "HEAD")
    return store.source


def link_beside(root, store, config):
    """Link each checkout app.links names beside the source worktree, where the project's relative path finds it: a
    project that builds another checkout's Swift packages names them by such a path (../NAME/src/swift/...), which
    from the store would otherwise lead nowhere. A link is the store's own; anything else already there is left
    alone."""
    for relative in config.get("app", {}).get("links", []): link(root, store, relative)


def link(root, store, relative):
    place = Path(os.path.normpath(store.source / relative))
    target = (repository(root) / relative).resolve()
    if store.path not in place.parents or not target.is_dir() or place == target: return
    if place.is_symlink():
        if Path(os.readlink(place)) == target: return
        place.unlink()
    elif place.exists():
        return
    place.symlink_to(target, target_is_directory=True)


def fetch(directory):
    """Fetch origin's main, saying so when origin cannot be reached and the known main must do."""
    done = git(directory, *FETCH, check=False, timeout=120)
    if done.returncode: say(f"could not fetch origin's main, so using the last one fetched: {done.stderr.strip()[-200:]}")


def resolve(directory, commit):
    return git(directory, "rev-parse", "--verify", f"{commit}^{{commit}}").stdout.strip()


def contains(directory, newer, older):
    """Whether newer holds older: newer is older or its descendant."""
    return git(directory, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def checkout(source, commit):
    """Put the source worktree at commit, with what the last build left there and tracks nothing removed."""
    git(source, "checkout", "--detach", "--force", commit)
    git(source, "clean", "-ffd")
    if (source / ".gitmodules").exists(): git(source, "submodule", "update", "--init", "--recursive")


def prepare_steps(app):
    """The commands app.prepare runs in order before each build: its one argv list, or each of its argv lists."""
    prepare = app.get("prepare")
    if not prepare: return []
    return prepare if all(isinstance(step, list) for step in prepare) else [prepare]


def build_command(app, platform, derived):
    """The xcodebuild that builds platform's app into derived."""
    given = app[platform]
    where = ["-workspace", app["workspace"]] if "workspace" in app else ["-project", app["project"]]
    command = ["xcodebuild", "build", *where, "-scheme", given["scheme"], "-configuration",
               app.get("configuration", "Debug"), "-destination", DESTINATIONS[platform], "-derivedDataPath",
               str(derived)]
    if platform == "ios": command.append("-allowProvisioningUpdates")
    return command + [f"{key}={value}" for key, value in sorted(given.get("settings", {}).items())]


# What xcodebuild says of a target that signs manually and names no profile. -allowProvisioningUpdates creates
# profiles only for a target that signs automatically, and a project generated by XcodeGen, which writes no
# CODE_SIGN_STYLE, signs manually: BreezEPUB's Quick Look extensions failed so (vocaro-engineering#206).
MANUAL_SIGNING = "requires a provisioning profile"


def signing_hint(log):
    """What to change when the build failed for a target that signs manually; empty otherwise."""
    try:
        with open(log, errors="replace") as handle:
            if not any(MANUAL_SIGNING in line for line in handle): return ""
    except OSError:
        return ""
    return (". A target signs manually, so no profile was made for it: sign it automatically, with "
            "CODE_SIGN_STYLE Automatic in the project or its project.yml (doc/guides/app-builds.md, Signing)")


def product(app, platform, derived):
    """The app the build of platform made in derived."""
    folder = derived / "Build/Products" / (app.get("configuration", "Debug") + PRODUCT_SUFFIX[platform])
    named = app[platform].get("product")
    if named:
        path = folder / named
        if not path.is_dir(): raise Failed(f"the build made no {named} in {folder}")
        return path
    found = sorted(path for path in folder.glob("*.app") if path.is_dir()) if folder.is_dir() else []
    if len(found) != 1:
        raise Failed(f"the build made {len(found)} apps in {folder}" + (f" ({', '.join(p.name for p in found)})" if found
                     else "") + f": name the one to keep in app.{platform}.product")
    return found[0]


def info(path):
    """The bundle's Info.plist, as a Mac app or a device app keeps it."""
    for plist in (path / "Contents/Info.plist", path / "Info.plist"):
        if plist.is_file():
            with plist.open("rb") as handle: return plistlib.load(handle)
    raise Failed(f"{path} holds no Info.plist")


def build(root, store, config, platform, commit, *, admit=None):
    """Build platform's app at commit in the store's source worktree and keep it; the kept build. Run with the
    store's lock held. admit runs the build under admission: by default the source checkout's `qk admit`."""
    source = ensure_source(root, store)
    checkout(source, commit)
    config = load(source)
    if "app" not in config or platform not in config["app"]:
        raise Failed(f"queuekeeper.json at {commit[:12]} configures no {platform} app")
    link_beside(root, store, config)
    app = config["app"]
    directory = source / app.get("directory", ".")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = store.logs / f"{stamp}-{platform}-{commit[:12]}.log"
    store.logs.mkdir(parents=True, exist_ok=True)
    steps = prepare_steps(app)
    for number, step in enumerate(steps, 1):
        say(f"preparing {platform} at {commit[:12]}: {' '.join(step)}")
        prepared = log.with_suffix(".prepare.log" if len(steps) == 1 else f".prepare-{number}.log")
        try:
            status = processes.run(step, cwd=directory, log_path=prepared, timeout=PREPARE_LIMIT)
        except processes.TimedOut as error:
            raise Failed(f"app.prepare reached its limit: {error}")
        if status: raise Failed(f"app.prepare's {step[0]} exited {status}; see {prepared}")
    derived = store.derived(platform)
    command = build_command(app, platform, derived)
    say(f"building {platform} at {commit[:12]}; log {log}")
    # --here: the build's product is this Mac's, kept in the store and run here (qk admit offers a command to another
    # Mac first otherwise).
    admit = admit or (lambda words: [sys.executable, str(source / "scripts/qk"), "admit", "--here", "--time-limit",
                                     str(BUILD_LIMIT), "--", *words])
    try:
        status = processes.run(admit(command), cwd=directory, log_path=log, timeout=BUILD_LIMIT * 4)
    except processes.TimedOut as error:
        raise Failed(f"the {platform} build reached its limit: {error}; see {log}")
    if status: raise Failed(f"the {platform} build at {commit[:12]} exited {status}; see {log}{signing_hint(log)}")
    built = product(app, platform, derived)
    bundle = info(built).get("CFBundleIdentifier")
    if not bundle: raise Failed(f"{built} names no CFBundleIdentifier")
    # By the short commit: forty hex digits in the device command's words would read as an older device's UDID.
    folder = store.path / "kept" / platform / commit[:12]
    staging = folder.with_name(folder.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # ditto keeps the bundle's signature, extended attributes and symbolic links as they were built.
    done = subprocess.run(["ditto", str(built), str(staging / built.name)], capture_output=True, text=True)
    if done.returncode: raise Failed(f"could not copy {built}: {done.stderr.strip()}")
    shutil.rmtree(folder, ignore_errors=True)
    os.replace(staging, folder)
    subject = git(source, "show", "-s", "--format=%s", commit).stdout.strip()
    kept = {"commit": commit, "subject": subject, "app": str(folder / built.name), "bundle": bundle,
            "configuration": app.get("configuration", "Debug"), "builtAt": datetime.now(timezone.utc).isoformat(),
            "log": str(log)}
    store.record(platform, kept)
    say(f"kept {platform} {built.name} at {commit[:12]}: {kept['app']}")
    return kept


def keep(root, config, *, commit=None, chosen=None, after=False, store=None, admit=None, wait=LOCK_WAIT):
    """Build and keep the app at commit, by default origin/main, for each platform chosen, by default every one
    the project builds, or with after only those kept after landing. A platform whose newest kept build already
    holds commit is skipped: a later landing kept it first. 0 when each kept or was current, 1 when any failed."""
    store = store or Store(config["name"])
    wanted = chosen or (after_landing(config) if after else platforms(config))
    if not wanted:
        say("nothing to keep: queuekeeper.json's app keeps no platform" + (" after landing" if after else ""))
        return 0
    failed = False
    with store.locked(wait, waiting=lambda: say("waiting for the keep under way")):
        source = ensure_source(root, store)
        if commit is None: fetch(source)
        target = resolve(source, commit or MAIN)
        for platform in wanted:
            newest = next(iter(store.kept(platform)), None)
            if newest and contains(source, newest["commit"], target):
                say(f"{platform}: the build kept at {newest['commit'][:12]} already holds {target[:12]}")
                continue
            try:
                build(root, store, config, platform, target, admit=admit)
            except Failed as error:
                say(f"{platform}: {error}. " + (f"The build kept at {newest['commit'][:12]} stays the one that runs"
                                                if newest else "No build of it is kept yet"))
                failed = True
    return 1 if failed else 0


def current(root, config, platform, *, store, build_missing=True, admit=None, wait=LOCK_WAIT):
    """The kept build of platform to run: one at origin/main, built first when the newest kept is older and
    build_missing, or else the newest kept, said so. Failed when there is none."""
    with store.locked(wait, waiting=lambda: say("waiting for the keep under way")):
        newest = next(iter(store.kept(platform)), None)
        if not build_missing:
            if newest is None: raise Failed(f"no {platform} build is kept yet: run `qk app keep`")
            return newest
        source = ensure_source(root, store)
        fetch(source)
        target = resolve(source, MAIN)
        if newest and contains(source, newest["commit"], target): return newest
        try:
            return build(root, store, config, platform, target, admit=admit)
        except Failed as error:
            if newest is None: raise
            behind = git(source, "rev-list", "--count", f"{newest['commit']}..{target}", check=False).stdout.strip()
            say(f"{error}. Running the build kept at {newest['commit'][:12]} instead, {behind or '?'} commits behind "
                "origin/main")
            return newest


def instances(bundle, run=subprocess.run):
    """{pid: app path} of each process running an app whose bundle identifier is bundle."""
    listing = run(["ps", "-axww", "-o", "pid=,comm="], capture_output=True, text=True).stdout
    found, read = {}, {}
    for line in listing.splitlines():
        pid, _, path = line.strip().partition(" ")
        if ".app/Contents/MacOS/" not in path or not pid.isdigit(): continue
        app = path.split(".app/Contents/MacOS/", 1)[0] + ".app"
        if app not in read:
            try:
                read[app] = info(Path(app)).get("CFBundleIdentifier")
            except (Failed, OSError, plistlib.InvalidFileException, ValueError):
                read[app] = None
        if read[app] == bundle: found[int(pid)] = app
    return found


def open_on_mac(kept, *, replace=False, run=subprocess.run, sleep=time.sleep):
    """Open the kept Mac build. Another copy of the app running elsewhere, such as a gate's test host, is
    quit first only with replace; otherwise Failed, naming it."""
    app, bundle = kept["app"], kept["bundle"]
    others = {pid: path for pid, path in instances(bundle, run).items()
              if os.path.realpath(path) != os.path.realpath(app)}
    if others and not replace:
        raise Failed(f"{bundle} already runs from " + ", ".join(sorted(set(others.values()))) + ", which may be a "
                     "gate's or a session's test run: quit it, or give --replace to have qk app quit it")
    if others:
        say(f"quitting {bundle}, which runs from {', '.join(sorted(set(others.values())))}")
        run(["osascript", "-e", f'tell application id "{bundle}" to quit'], capture_output=True, text=True)
        for _ in range(QUIT_WAIT):
            if not {pid for pid in instances(bundle, run) if pid in others}: break
            sleep(1)
        else:
            raise Failed(f"{bundle} did not quit within {QUIT_WAIT}s; nothing was opened")
    done = run(["open", app], capture_output=True, text=True)
    if done.returncode: raise Failed(f"open {app} failed: {done.stderr.strip()}")
    say(f"opened {Path(app).name} built at {kept['commit'][:12]} ({kept.get('subject', '')})")


def device_command(kept):
    """The command a device pass runs to install the kept build on {udid} and launch it."""
    return ["env", "PYTHONPATH=" + os.pathsep.join(map(str, import_paths("queuekeeper", "queuekeeper_apple", "queuekeeper_mac_desktop", "queuekeeper_mobile"))), sys.executable, "-m", "queuekeeper_apple.app_builds",
            "device", "--udid", "{udid}", "--bundle", kept["bundle"], kept["app"]]


def install_and_launch(udid, app, bundle, run=subprocess.run, sleep=time.sleep):
    """Install app on the device udid and launch it, ending one already running; the exit status."""
    try:
        done = run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, app], capture_output=True,
                   text=True, timeout=INSTALL_LIMIT)
    except subprocess.TimeoutExpired:
        say(f"the install did not end within {INSTALL_LIMIT}s, so whether it installed is unknown")
        return 1
    if done.returncode:
        say(f"the install failed: {(done.stderr or done.stdout).strip()[-400:]}")
        return 1
    say(f"installed {Path(app).name}")
    for attempt in (1, 2):
        try:
            done = run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, "--terminate-existing",
                        "--", bundle], capture_output=True, text=True, timeout=LAUNCH_LIMIT)
        except subprocess.TimeoutExpired:
            done = subprocess.CompletedProcess([], 124, "", f"no answer within {LAUNCH_LIMIT}s")
        if done.returncode == 0:
            say(f"launched {bundle}")
            return 0
        said = (done.stderr or done.stdout).strip()[-400:]
        if attempt == 1:
            say(f"the launch failed, so trying once more: {said}")
            sleep(5)
    say(f"the launch failed: {said}")
    return 1


def run_on_device(kept, key, *, owner, wait=device_pass.WAIT, record=None, pass_run=device_pass.run):
    """Install and launch the kept device build on key's device under its reservation; the exit status."""
    parts, code = pass_run([key], device_command(kept), owner=owner, purpose=f"qk app run of {Path(kept['app']).name}",
                           minutes=DEVICE_MINUTES, wait=wait, launched=[kept["bundle"]],
                           apps_quit="left running on the device for the owner to use", record=record,
                           said=say, results=None)
    for part in parts:
        if part.outcome != device_pass.PASSED: say(f"{key}: {part.outcome}: {part.detail}")
    if code == 0: say(f"{key} runs {Path(kept['app']).name} built at {kept['commit'][:12]} ({kept.get('subject', '')})")
    return code


def owner_of(given):
    return given or devices.default_owner() or f"qk app ({getpass.getuser()})"


def describe(store, config, root):
    """Each platform's kept builds and how far each is behind origin/main, for `qk app status`."""
    lines = [f"{config['name']}: {store.path}" + (" (a keep is building now)" if store.busy() else "")]
    source = store.source if (store.source / ".git").exists() else root
    for platform in platforms(config):
        builds = store.kept(platform)
        if not builds:
            lines.append(f"  {platform}: nothing kept yet")
            continue
        for index, kept in enumerate(builds):
            behind = git(source, "rev-list", "--count", f"{kept['commit']}..{MAIN}", check=False).stdout.strip()
            lines.append(f"  {platform}{' (previous)' if index else ''}: {kept['commit'][:12]} {kept.get('subject', '')}"
                         f", built {kept.get('builtAt', '?')}, {behind or '?'} commits behind origin/main: {kept['app']}")
    return lines


def spawn_keep(checkout, commit, log, env=None):
    """Start `qk app keep --after-landing --commit COMMIT` from checkout in the background, writing to log, for
    the merge queue after a push; it outlives the worker's round and holds none of its locks."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        return subprocess.Popen([sys.executable, str(Path(checkout) / "scripts/qk"), "app", "keep", "--after-landing",
                                 "--commit", commit], cwd=checkout, stdin=subprocess.DEVNULL, stdout=handle,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True, metavar="{run,keep,status}")
    running = operations.add_parser("run", help="open the newest kept Mac build, or install and launch the device "
        "build on the paired iPhone or iPad, building origin/main first when the newest kept build is older")
    target = running.add_mutually_exclusive_group()
    target.add_argument("--iphone", action="store_true", help="install and launch on the paired iPhone, taken in "
                        "turn; ask the owner first, since it is his personal phone")
    target.add_argument("--ipad", action="store_true", help="install and launch on the paired iPad, taken in turn")
    running.add_argument("--no-build", action="store_true", help="run the newest kept build as it is, building "
                         "nothing")
    running.add_argument("--replace", action="store_true", help="on this Mac, quit another copy of the app that runs "
                         "from elsewhere, such as a test run's, before opening the kept one")
    running.add_argument("--owner", help="the session the device's reservation names (default: QUEUEKEEPER_DEVICE_OWNER, "
                         "else the agent session, else this account)")
    running.add_argument("--wait", type=float, default=device_pass.WAIT, metavar="SECONDS", help="how long to wait "
                         f"in the device's line (default {device_pass.WAIT})")
    keeping = operations.add_parser("keep", help="build the app at a commit, by default origin/main, and keep it "
                                    "as the newest build when it builds")
    keeping.add_argument("--commit", help="the commit to build (default origin/main, fetched)")
    keeping.add_argument("--platform", action="append", choices=sorted(APP_PLATFORMS), help="a platform to build "
                         "(repeatable; default every one the app builds)")
    keeping.add_argument("--after-landing", action="store_true", help="only the platforms kept after each landing, "
                         "as the merge queue runs it")
    operations.add_parser("status", help="list the kept builds and how far each is behind origin/main")
    # The step a device pass runs on the reserved device (device_command), not a command of its own.
    device = operations.add_parser("device")
    device.add_argument("--udid", required=True)
    device.add_argument("--bundle", required=True)
    device.add_argument("app")


def behind_main(root):
    """What to say when this checkout's queuekeeper.json lacks `app` but origin/main's has one: the checkout is
    behind, as the owner's shared checkout can be, and `add app` would send its reader the wrong way. Empty when
    origin/main configures no app either, or cannot be read; it is not fetched, so as to read nothing new."""
    if root is None: return ""
    try:
        shown = git(root, "show", f"{MAIN}:queuekeeper.json", check=False)
        if shown.returncode or "app" not in json.loads(shown.stdout): return ""
        behind = git(root, "rev-list", "--count", f"HEAD..{MAIN}", check=False).stdout.strip() or "some"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    return (f" at this checkout, which is {behind} commit(s) behind origin/main, whose queuekeeper.json does: "
            f"update the checkout, or run qk app from a worktree at origin/main")


def execute(root, args, config=None):
    if args.operation == "device": return install_and_launch(args.udid, args.app, args.bundle)
    config = config if config is not None else load(root)
    if "app" not in config:
        say("queuekeeper.json configures no app" + (behind_main(root) or ": add `app` (doc/guides/app-builds.md)"))
        return 2
    store = Store(config["name"])
    try:
        if args.operation == "status":
            print("\n".join(describe(store, config, root)))
            return 0
        if args.operation == "keep":
            chosen = [name for name in platforms(config) if name in (args.platform or [])]
            if args.platform and not chosen:
                raise Failed(f"the app builds no {', '.join(args.platform)}; it builds {', '.join(platforms(config))}")
            with processes.forwarding_signals():
                return keep(root, config, commit=args.commit, chosen=chosen or None, after=args.after_landing,
                            store=store)
        key = DEVICES["iphone"] if args.iphone else DEVICES["ipad"] if args.ipad else None
        platform = "ios" if key else "mac"
        if platform not in config["app"]:
            raise Failed(f"queuekeeper.json's app builds no {platform} app")
        with processes.forwarding_signals():
            kept = current(root, config, platform, store=store, build_missing=not args.no_build)
            if key is None:
                open_on_mac(kept, replace=args.replace)
                return 0
            return run_on_device(kept, key, owner=owner_of(args.owner), wait=args.wait)
    except processes.Interrupted:
        say("stopped")
        return 130
    except (Failed, TimeoutError, OSError, ValueError, devices.Refused, device_pass.Misplaced) as error:
        say(str(error))
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m queuekeeper_apple.app_builds")
    add_arguments(parser)
    args = parser.parse_args(argv)
    if args.operation != "device": parser.error("run `qk app` from the project instead")
    return execute(None, args)


if __name__ == "__main__":
    raise SystemExit(main())
