"""The last good build of a project's app, kept and run: `qk app` (app_builds.py)."""
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import host_isolation  # noqa: F401  (first: keeps every test out of the host's admission)
from queuekeeper import cli, config, disk, merge_queue
from queuekeeper_apple import disk as apple_disk
from queuekeeper_apple import app_builds
from queuekeeper_mobile import device_pass

BUNDLE = "com.example.demo"
# A stand-in for `qk admit -- xcodebuild ...`: it makes the product xcodebuild would, Demo.app with its Info.plist,
# in the -derivedDataPath it was given, or exits 65 as a failed build does when FAIL names a file that exists,
# printing what the file holds.
BUILDER = f"""
import os, pathlib, plistlib, sys
words = sys.argv[1:]
fail = os.environ.get("APP_BUILD_FAIL")
if fail and os.path.exists(fail):
    print(open(fail).read())
    raise SystemExit(65)
derived = pathlib.Path(words[words.index("-derivedDataPath") + 1])
ios = "generic/platform=iOS" in words
folder = derived / "Build/Products" / ("Debug-iphoneos" if ios else "Debug") / "Demo.app"
plist = folder / "Info.plist" if ios else folder / "Contents/Info.plist"
plist.parent.mkdir(parents=True, exist_ok=True)
plist.write_bytes(plistlib.dumps({{"CFBundleIdentifier": "{BUNDLE}"}}))
open(os.environ["APP_BUILD_CALLS"], "a").write(" ".join(words) + "\\n")
"""
APP = {"project": "Demo.xcodeproj", "mac": {"scheme": "Demo"}, "ios": {"scheme": "Demo"}}


def project(app=APP):
    return {"schemaVersion": 1, "name": "Demo", "app": app}


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


class Validation(unittest.TestCase):
    def parse(self, app):
        return config.parse(json.dumps(project(app)))

    def test_an_app_with_a_project_and_a_platform_is_accepted(self):
        self.assertEqual(self.parse(APP)["app"], APP)
        self.parse({"workspace": "Demo.xcworkspace", "directory": "app", "configuration": "Release",
                    "prepare": ["xcodegen", "generate"], "mac": {"scheme": "Demo", "product": "Demo.app",
                    "settings": {"DEVELOPMENT_TEAM": "ABC"}, "afterLanding": False}})
        # Or a list of commands, run in order.
        self.parse({**APP, "prepare": [["xcodegen", "generate"], ["python3", "lock.py"]]})

    def test_what_an_app_may_not_say_is_refused(self):
        for app, words in [
                ({"mac": {"scheme": "Demo"}}, "project or its workspace"),
                ({"project": "A.xcodeproj", "workspace": "A.xcworkspace", "mac": {"scheme": "Demo"}}, "one of them"),
                ({"project": "A.xcodeproj"}, "at least one platform"),
                ({"project": "A.xcodeproj", "tvos": {"scheme": "Demo"}}, "supports only"),
                ({"project": "A.xcodeproj", "mac": {}}, "scheme"),
                ({"project": "A.xcodeproj", "mac": {"scheme": "Demo", "product": "Demo"}}, "file name"),
                ({"project": "A.xcodeproj", "mac": {"scheme": "Demo", "settings": {"x": "1"}}}, "build setting"),
                ({"project": "A.xcodeproj", "mac": {"scheme": "Demo", "afterLanding": "yes"}}, "true or false"),
                ({"project": "A.xcodeproj", "prepare": "xcodegen generate", "mac": {"scheme": "Demo"}}, "argv"),
                ({"project": "A.xcodeproj", "prepare": [], "mac": {"scheme": "Demo"}}, "argv"),
                ({"project": "A.xcodeproj", "prepare": [["xcodegen"], []], "mac": {"scheme": "Demo"}}, "argv"),
                ({"project": "A.xcodeproj", "prepare": [["xcodegen"], "generate"], "mac": {"scheme": "Demo"}}, "argv"),
                ({"project": "A.xcodeproj", "prepare": [["xcodegen", ""]], "mac": {"scheme": "Demo"}}, "argv"),
                ({"project": "../A.xcodeproj", "mac": {"scheme": "Demo"}}, "inside the checkout")]:
            with self.subTest(app=app), self.assertRaisesRegex(ValueError, words):
                self.parse(app)

    def test_the_mac_build_is_kept_after_landing_and_a_device_build_only_when_asked(self):
        self.assertEqual(app_builds.after_landing(project()), ["mac"])
        self.assertEqual(app_builds.after_landing(project({**APP, "mac": {"scheme": "Demo", "afterLanding": False},
                                                           "ios": {"scheme": "Demo", "afterLanding": True}})), ["ios"])
        self.assertEqual(app_builds.after_landing({"name": "x"}), [])

    def test_the_build_command(self):
        command = app_builds.build_command({**APP, "ios": {"scheme": "Demo", "settings": {"B": "2", "A": "1"}}}, "ios",
                                           Path("/d"))
        self.assertEqual(command, ["xcodebuild", "build", "-project", "Demo.xcodeproj", "-scheme", "Demo",
                                   "-configuration", "Debug", "-destination", "generic/platform=iOS",
                                   "-derivedDataPath", "/d", "-allowProvisioningUpdates", "A=1", "B=2"])
        self.assertNotIn("-allowProvisioningUpdates", app_builds.build_command(APP, "mac", Path("/d")))


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.origin = self.temp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.root = self.temp / "Demo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.root)], check=True, capture_output=True)
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "T")
        self.calls = self.temp / "calls"
        self.broken = self.temp / "fail"
        for patcher in (patch.dict(os.environ, {"APP_BUILD_CALLS": str(self.calls), "APP_BUILD_FAIL": str(self.broken)}),
                        patch.object(app_builds, "say", lambda text: self.said.append(text))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.said = []
        self.store = app_builds.Store("Demo", self.temp / "store")
        self.commit("first")
        self.addCleanup(lambda: subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", "--force",
                                                str(self.store.source)], capture_output=True))

    def commit(self, subject, app=APP):
        (self.root / "queuekeeper.json").write_text(json.dumps(project(app)))
        (self.root / "change").write_text(subject)
        git(self.root, "add", "queuekeeper.json", "change")
        git(self.root, "commit", "-q", "-m", subject)
        git(self.root, "push", "-q", "origin", "HEAD:main")
        return git(self.root, "rev-parse", "HEAD")

    @staticmethod
    def admit(words):
        return [sys.executable, "-c", BUILDER, *words]

    def builds(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def keep(self, app=APP, **options):
        return app_builds.keep(self.root, project(app), store=self.store, admit=self.admit, **options)


class Keeping(Fixture):
    def test_a_landed_commit_is_built_in_the_stores_own_locked_worktree_and_kept(self):
        head = git(self.root, "rev-parse", "HEAD")
        self.assertEqual(self.keep(commit=head, after=True), 0, self.said)
        [kept] = self.store.kept("mac")
        self.assertEqual((kept["commit"], kept["bundle"], kept["subject"]), (head, BUNDLE, "first"))
        self.assertEqual(Path(kept["app"]), self.store.path / "kept/mac" / head[:12] / "Demo.app")
        self.assertTrue((Path(kept["app"]) / "Contents/Info.plist").is_file())
        # Only the Mac is kept after landing; the build ran in the store's worktree, at the commit, into the store's
        # DerivedData for the platform.
        [build] = self.builds()
        self.assertIn(f"-derivedDataPath {self.store.derived('mac')}", build)
        self.assertEqual(self.store.kept("ios"), [])
        self.assertEqual(git(self.store.source, "rev-parse", "HEAD"), head)
        listing = git(self.root, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {self.store.source}\nHEAD {head}\ndetached\nlocked {app_builds.SOURCE_REASON}", listing)

    def test_a_checkout_the_app_links_is_linked_where_the_projects_path_finds_it(self):
        # A project names another checkout's Swift packages by ../toolkit, which from the store leads nowhere.
        toolkit = self.temp / "toolkit"
        toolkit.mkdir()
        linked = {**APP, "links": ["../toolkit"]}
        link = self.store.path / "toolkit"
        # The control: an app that links nothing gets no link.
        self.keep(chosen=["mac"])
        self.assertFalse(link.exists() or link.is_symlink())
        self.keep(linked, commit=self.commit("linked", linked), chosen=["mac"])
        self.assertEqual((link.is_symlink(), link.resolve()), (True, toolkit.resolve()))
        # It stays across builds, and something else already there is left alone.
        self.keep(linked, commit=self.commit("second", linked), chosen=["mac"])
        self.assertTrue(link.is_symlink())
        link.unlink()
        link.mkdir()
        self.keep(linked, commit=self.commit("third", linked), chosen=["mac"])
        self.assertFalse(link.is_symlink())

    def test_a_commit_the_newest_kept_build_holds_is_not_built_again(self):
        older = git(self.root, "rev-parse", "HEAD")
        newer = self.commit("second")
        self.assertEqual(self.keep(commit=newer), 0)
        self.assertEqual(len(self.builds()), 2)  # mac and ios
        # A landing's keep that comes after a later one's has nothing to do.
        self.assertEqual(self.keep(commit=older, chosen=["mac"]), 0)
        self.assertEqual(len(self.builds()), 2)
        self.assertTrue(any("already holds" in line for line in self.said))

    def test_a_failed_build_keeps_nothing_and_the_last_good_build_stays(self):
        good = git(self.root, "rev-parse", "HEAD")
        self.keep(commit=good, chosen=["mac"])
        bad = self.commit("broken")
        self.broken.write_text("")
        self.assertEqual(self.keep(commit=bad, chosen=["mac"]), 1)
        self.assertEqual([build["commit"] for build in self.store.kept("mac")], [good])
        self.assertTrue(any("exited 65" in line and good[:12] in line for line in self.said), self.said)

    def test_a_target_that_signs_manually_is_named_as_the_cause_and_another_failure_is_not(self):
        # xcodebuild's words for BreezEPUB's Quick Look extensions, which signed manually (vocaro-engineering#206).
        self.broken.write_text('BreezEPUB.xcodeproj: error: "BreezEPUBThumbnail" requires a provisioning profile. '
                               "Select a provisioning profile in the Signing & Capabilities editor.")
        self.assertEqual(self.keep(chosen=["ios"]), 1)
        [failure] = [line for line in self.said if "exited 65" in line]
        self.assertIn("CODE_SIGN_STYLE Automatic", failure)
        self.said.clear()
        self.broken.write_text("error: cannot find 'Thing' in scope")
        self.assertEqual(self.keep(chosen=["ios"]), 1)
        [failure] = [line for line in self.said if "exited 65" in line]
        self.assertNotIn("CODE_SIGN_STYLE", failure)

    def test_the_newest_two_of_each_platform_are_kept_and_older_ones_removed(self):
        commits = [git(self.root, "rev-parse", "HEAD"), self.commit("second"), self.commit("third")]
        for commit in commits: self.keep(commit=commit, chosen=["mac"])
        kept = self.store.kept("mac")
        self.assertEqual([build["commit"] for build in kept], commits[:0:-1])
        self.assertFalse((self.store.path / "kept/mac" / commits[0][:12]).exists())

    def test_a_platform_the_commits_engineering_json_drops_is_not_built(self):
        head = self.commit("no ios", app={"project": "Demo.xcodeproj", "mac": {"scheme": "Demo"}})
        self.assertEqual(self.keep(commit=head, chosen=["ios"]), 1)
        self.assertTrue(any("configures no ios app" in line for line in self.said), self.said)

    def test_a_prepare_step_runs_before_the_build_and_its_failure_keeps_nothing(self):
        marker = self.temp / "prepared"
        app = {**APP, "prepare": [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]}
        head = self.commit("prepared", app=app)
        self.assertEqual(app_builds.keep(self.root, project(app), store=self.store, admit=self.admit, commit=head,
                                         chosen=["mac"]), 0)
        self.assertTrue(marker.exists())
        app["prepare"] = [sys.executable, "-c", "raise SystemExit(3)"]
        head = self.commit("prepare fails", app=app)
        self.assertEqual(app_builds.keep(self.root, project(app), store=self.store, admit=self.admit, commit=head,
                                         chosen=["mac"]), 1)
        self.assertEqual(len(self.store.kept("mac")), 1)

    def test_a_list_of_prepare_commands_runs_in_order_and_a_failing_one_stops_the_rest_and_the_build(self):
        # As a generated project's lock is seeded after xcodegen makes the project.
        trace = self.temp / "trace"
        step = lambda name, code=0: [sys.executable, "-c",
                                     f"open({str(trace)!r}, 'a').write({name!r} + ' ' + __import__('os').getcwd()"
                                     f" + '\\n'); raise SystemExit({code})"]
        (self.root / "app").mkdir()
        (self.root / "app/keep").write_text("")
        git(self.root, "add", "app/keep")
        app = {**APP, "directory": "app", "prepare": [step("generate"), step("lock")]}
        head = self.commit("two steps", app=app)
        self.assertEqual(app_builds.keep(self.root, project(app), store=self.store, admit=self.admit, commit=head,
                                         chosen=["mac"]), 0, self.said)
        here = str((self.store.source / "app").resolve())
        self.assertEqual([line.split(" ", 1) for line in trace.read_text().splitlines()],
                         [["generate", here], ["lock", here]])
        self.assertEqual(len(self.builds()), 1)
        trace.unlink()
        app["prepare"] = [step("generate", 3), step("lock")]
        head = self.commit("first step fails", app=app)
        self.assertEqual(app_builds.keep(self.root, project(app), store=self.store, admit=self.admit, commit=head,
                                         chosen=["mac"]), 1)
        self.assertEqual(trace.read_text().split(), ["generate", here])
        self.assertEqual(len(self.builds()), 1)
        self.assertTrue(any("exited 3" in line and "prepare-1.log" in line for line in self.said), self.said)

    def test_two_products_need_the_one_to_keep_named(self):
        derived = self.temp / "derived"
        for name in ("A.app", "B.app"): (derived / "Build/Products/Debug" / name).mkdir(parents=True)
        with self.assertRaisesRegex(app_builds.Failed, "app.mac.product"):
            app_builds.product(APP, "mac", derived)
        named = {**APP, "mac": {"scheme": "Demo", "product": "B.app"}}
        self.assertEqual(app_builds.product(named, "mac", derived).name, "B.app")


class Current(Fixture):
    def test_a_kept_build_at_origin_main_runs_without_building(self):
        self.keep(chosen=["mac"])
        kept = app_builds.current(self.root, project(), "mac", store=self.store, admit=self.admit)
        self.assertEqual(kept["commit"], git(self.root, "rev-parse", "origin/main"))
        self.assertEqual(len(self.builds()), 1)

    def test_an_older_kept_build_is_brought_to_origin_main_first(self):
        self.keep(chosen=["mac"])
        head = self.commit("second")
        kept = app_builds.current(self.root, project(), "mac", store=self.store, admit=self.admit)
        self.assertEqual(kept["commit"], head)

    def test_when_main_does_not_build_the_newest_kept_build_runs_and_says_so(self):
        self.keep(chosen=["mac"])
        good = git(self.root, "rev-parse", "HEAD")
        self.commit("broken")
        self.broken.write_text("")
        kept = app_builds.current(self.root, project(), "mac", store=self.store, admit=self.admit)
        self.assertEqual(kept["commit"], good)
        self.assertTrue(any(f"Running the build kept at {good[:12]} instead, 1 commits behind" in line
                            for line in self.said), self.said)

    def test_no_build_runs_the_newest_kept_as_it_is_and_fails_with_none(self):
        with self.assertRaisesRegex(app_builds.Failed, "no ios build is kept yet"):
            app_builds.current(self.root, project(), "ios", store=self.store, build_missing=False)
        self.keep(chosen=["ios"])
        self.commit("second")
        kept = app_builds.current(self.root, project(), "ios", store=self.store, build_missing=False)
        self.assertEqual(kept["subject"], "first")

    def test_status_lists_each_platforms_builds_and_how_far_behind_they_are(self):
        self.keep(chosen=["mac"])
        self.commit("second")
        git(self.root, "fetch", "-q", "origin")
        lines = app_builds.describe(self.store, project(), self.root)
        self.assertIn("1 commits behind origin/main", lines[1])
        self.assertEqual(lines[2], "  ios: nothing kept yet")


class Configured(Fixture):
    def status(self):
        return app_builds.execute(self.root, SimpleNamespace(operation="status"))

    def test_a_checkout_behind_an_origin_main_that_configures_app_says_so(self):
        # The owner's shared Pagepour checkout was behind the commit that added `app` (vocaro-engineering#206).
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Demo"}))
        git(self.root, "commit", "-q", "-am", "no app")
        git(self.root, "push", "-q", "origin", "HEAD:main")
        self.commit("app again")
        self.commit("later")
        git(self.root, "reset", "-q", "--hard", "HEAD~2")
        self.assertEqual(self.status(), 2)
        [said] = self.said
        self.assertIn("2 commit(s) behind origin/main", said)
        self.assertNotIn("add `app`", said)

    def test_a_project_whose_origin_main_configures_no_app_is_told_to_add_it(self):
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Demo"}))
        git(self.root, "commit", "-q", "-am", "no app")
        git(self.root, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.status(), 2)
        self.assertEqual(self.said, ["queuekeeper.json configures no app: add `app` (doc/guides/app-builds.md)"])


class Ps:
    """ps's listing and each command run, with apps whose Info.plists the listing names."""

    def __init__(self, listing):
        self.listing, self.calls = listing, []

    def __call__(self, command, **_options):
        self.calls.append(command)
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, self.listing() if callable(self.listing) else self.listing, "")
        return subprocess.CompletedProcess(command, 0, "", "")


class Launching(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.kept = self.app("kept/Demo.app")
        self.other = self.app("DerivedData/Demo.app")
        self.said = []
        patcher = patch.object(app_builds, "say", self.said.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, relative, bundle=BUNDLE):
        path = self.temp / relative
        (path / "Contents/MacOS").mkdir(parents=True)
        (path / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle}))
        return path

    def build(self):
        return {"app": str(self.kept), "bundle": BUNDLE, "commit": "a" * 40, "subject": "s"}

    def test_the_kept_build_opens_and_another_app_is_not_counted(self):
        unrelated = self.app("Other.app", bundle="com.example.other")
        run = Ps(f"  1 {unrelated}/Contents/MacOS/Other\n  2 {self.kept}/Contents/MacOS/Demo\n")
        app_builds.open_on_mac(self.build(), run=run)
        self.assertEqual(run.calls[-1], ["open", str(self.kept)])

    def test_another_copy_running_refuses_unless_replace_quits_it(self):
        run = Ps(f"  7 {self.other}/Contents/MacOS/Demo\n")
        with self.assertRaisesRegex(app_builds.Failed, "already runs from .*DerivedData"):
            app_builds.open_on_mac(self.build(), run=run)
        self.assertNotIn("open", [call[0] for call in run.calls])
        listings = iter([f"  7 {self.other}/Contents/MacOS/Demo\n", ""])
        run = Ps(lambda: next(listings, ""))
        app_builds.open_on_mac(self.build(), replace=True, run=run, sleep=lambda _: None)
        self.assertEqual([call[0] for call in run.calls], ["ps", "osascript", "ps", "open"])
        self.assertIn(f'tell application id "{BUNDLE}" to quit', run.calls[1])

    def test_a_copy_that_will_not_quit_opens_nothing(self):
        run = Ps(f"  7 {self.other}/Contents/MacOS/Demo\n")
        with self.assertRaisesRegex(app_builds.Failed, "did not quit"):
            app_builds.open_on_mac(self.build(), replace=True, run=run, sleep=lambda _: None)
        self.assertNotIn("open", [call[0] for call in run.calls])


class Devicectl:
    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, command, **_options):
        self.calls.append(command)
        status = self.statuses.pop(0)
        if status == "timeout": raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, status, "", "CoreDeviceError 1010" if status else "")


class Devices(unittest.TestCase):
    def setUp(self):
        self.said = []
        patcher = patch.object(app_builds, "say", self.said.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_then_launch_ending_the_running_copy(self):
        run = Devicectl(0, 0)
        self.assertEqual(app_builds.install_and_launch("U", "/k/Demo.app", BUNDLE, run=run), 0)
        self.assertEqual(run.calls, [["xcrun", "devicectl", "device", "install", "app", "--device", "U", "/k/Demo.app"],
                                     ["xcrun", "devicectl", "device", "process", "launch", "--device", "U",
                                      "--terminate-existing", "--", BUNDLE]])

    def test_a_first_launch_that_fails_is_tried_once_more(self):
        self.assertEqual(app_builds.install_and_launch("U", "/a", BUNDLE, run=Devicectl(0, 1, 0), sleep=lambda _: None), 0)
        self.assertEqual(app_builds.install_and_launch("U", "/a", BUNDLE, run=Devicectl(0, 1, 1), sleep=lambda _: None), 1)

    def test_a_failed_or_unfinished_install_launches_nothing(self):
        for status in (1, "timeout"):
            run = Devicectl(status)
            self.assertEqual(app_builds.install_and_launch("U", "/a", BUNDLE, run=run), 1)
            self.assertEqual(len(run.calls), 1)

    def test_a_device_run_is_a_device_pass_on_the_one_device(self):
        seen = {}
        def pass_run(keys, command, **options):
            seen.update(keys=keys, command=command, **options)
            return [device_pass.Part(keys[0], device_pass.PASSED, "exit 0", 0)], 0
        kept = {"app": "/store/kept/ios/abc/Demo.app", "bundle": BUNDLE, "commit": "a" * 40}
        code = app_builds.run_on_device(kept, "iPhone", owner="s", pass_run=pass_run)
        self.assertEqual(code, 0)
        self.assertEqual((seen["keys"], seen["owner"], seen["launched"]), (["iPhone"], "s", [BUNDLE]))
        self.assertNotIn("approval", seen)
        self.assertIn("{udid}", seen["command"])
        self.assertEqual(seen["command"][-1], kept["app"])
        self.assertTrue(seen["apps_quit"])

    def test_the_device_step_runs_as_a_module_from_the_toolkit(self):
        command = app_builds.device_command({"app": "/a/Demo.app", "bundle": BUNDLE})
        self.assertEqual(command[5:], ["device", "--udid", "{udid}", "--bundle", BUNDLE, "/a/Demo.app"])
        done = subprocess.run(command[:6] + ["-h"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_command_line_refuses_an_approval_and_an_app_it_does_not_build(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "queuekeeper.json").write_text(json.dumps(project({"project": "A.xcodeproj", "mac": {"scheme": "A"}})))
        # The owner's decision of 2026-11-06: the device's reservation records no approval, so qk app takes none.
        with contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.main(["--project", str(root), "app", "run", "--iphone", "--approved-by", "owner"])
        self.assertEqual(cli.main(["--project", str(root), "app", "run", "--ipad"]), 2)
        self.assertTrue(any("builds no ios app" in line for line in self.said), self.said)
        (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "A"}))
        self.assertEqual(cli.main(["--project", str(root), "app", "status"]), 2)
        self.assertTrue(any("configures no app" in line for line in self.said), self.said)


class Queue(Fixture):
    def worker(self):
        said = []
        worker = SimpleNamespace(queue=SimpleNamespace(logs=self.temp / "logs"), checkout=self.root,
                                 environment={"A": "1"}, say=said.append)
        return worker, said

    def test_a_gated_push_starts_keeping_the_app_by_the_pushed_commits_settings(self):
        # The commit that first gives app keeps its build: main's settings at the batch's base had none.
        worker, said = self.worker()
        head = git(self.root, "rev-parse", "HEAD")
        with patch.object(app_builds, "spawn_keep") as spawn:
            merge_queue.Worker.keep_app(worker, head)
        spawn.assert_called_once_with(self.root, head, worker.queue.logs / f"app-keep-{head[:12]}.log", env={"A": "1"})
        self.assertIn("in the background", said[0])

    def test_a_commit_that_keeps_nothing_after_landing_starts_nothing(self):
        worker, said = self.worker()
        none = self.commit("no keeping", app={**APP, "mac": {"scheme": "D", "afterLanding": False}})
        (self.root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Demo"}))
        git(self.root, "commit", "-q", "-am", "no app")
        with patch.object(app_builds, "spawn_keep") as spawn:
            merge_queue.Worker.keep_app(worker, none)
            merge_queue.Worker.keep_app(worker, git(self.root, "rev-parse", "HEAD"))
        spawn.assert_not_called()
        self.assertEqual(said, [])

    def test_a_pushed_docs_configuration_is_accepted_when_keeping_an_app_or_no_app(self):
        # The worker at 4905b91 parsed the newly pushed configuration with its older, in-memory parser,
        # which did not know docs yet (#146). Retention must accept the same settings as the queue.
        for keeps_app in (True, False):
            with self.subTest(keeps_app=keeps_app):
                settings = {**project(), "docs": {"citations": True}}
                if not keeps_app: settings.pop("app")
                (self.root / "queuekeeper.json").write_text(json.dumps(settings))
                git(self.root, "commit", "-q", "-am", f"docs, app={keeps_app}")
                git(self.root, "push", "-q", "origin", "HEAD:main")
                head = git(self.root, "rev-parse", "HEAD")
                self.assertEqual(merge_queue.config_at(self.root, head), settings)
                worker, said = self.worker()
                with patch.object(app_builds, "spawn_keep") as spawn:
                    merge_queue.Worker.keep_app(worker, head)
                if keeps_app:
                    spawn.assert_called_once_with(self.root, head, worker.queue.logs / f"app-keep-{head[:12]}.log",
                                                  env={"A": "1"})
                    self.assertEqual(len(said), 1)
                    self.assertIn("in the background", said[0])
                else:
                    spawn.assert_not_called()
                    self.assertEqual(said, [])

    def test_a_pushed_invalid_configuration_still_reports_why_no_app_is_kept(self):
        for extra, reason in (({"docs": {"citations": "yes"}}, "docs.citations must be true or false"),
                              ({"unknown": True}, "Unknown configuration keys: ['unknown']")):
            with self.subTest(extra=extra):
                (self.root / "queuekeeper.json").write_text(json.dumps({**project(), **extra}))
                git(self.root, "commit", "-q", "-am", "invalid settings")
                git(self.root, "push", "-q", "origin", "HEAD:main")
                head = git(self.root, "rev-parse", "HEAD")
                worker, said = self.worker()
                with patch.object(app_builds, "spawn_keep") as spawn:
                    merge_queue.Worker.keep_app(worker, head)
                spawn.assert_not_called()
                self.assertEqual(said, [f"keeping no app build of {head[:12]}: {reason}"])

    def test_the_spawned_keep_outlives_the_round_and_writes_its_log(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        checkout = Path(temp.name)
        (checkout / "scripts").mkdir()
        (checkout / "scripts/qk").write_text("import sys; print(' '.join(sys.argv[1:]))\n")
        process = app_builds.spawn_keep(checkout, "c" * 40, checkout / "logs/keep.log")
        self.assertEqual(process.wait(timeout=host_isolation.stretched(30)), 0)
        self.assertEqual((checkout / "logs/keep.log").read_text(), f"app keep --after-landing --commit {'c' * 40}\n")


class Disk(unittest.TestCase):
    def test_kept_builds_stay_and_an_idle_build_cache_is_a_candidate(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        store = home / apple_disk.APP_STORE / "Demo"
        (store / "kept/mac/abc/Demo.app").mkdir(parents=True)
        (store / "derived/mac").mkdir(parents=True)
        old = time.time() - 3 * 86400
        for path in (store / "derived/mac", store / "kept/mac/abc/Demo.app"): os.utime(path, (old, old))
        host = SimpleNamespace(home=home, now=time.time(), min_idle=86400)
        items = apple_disk.app_builds(host, disk.Usage([]), disk.Sizes(measure=False))
        self.assertEqual([(Path(item.path).relative_to(store).as_posix(), item.status) for item in items],
                         [("kept", disk.KEPT), ("derived/mac", disk.CANDIDATE)])
        # A keep under way holds the store's lock, which keeps its cache: a build's arguments name a path with a
        # space in it, Application Support, that no census of arguments reads whole.
        with app_builds.Store("Demo", home / apple_disk.APP_STORE).locked():
            [_, cache] = apple_disk.app_builds(host, disk.Usage([]), disk.Sizes(measure=False))
        self.assertEqual((cache.status, cache.reason), (disk.KEPT, "a keep is building with it now"))
        self.assertEqual(apple_disk.app_builds(SimpleNamespace(home=home / "none"), None, None), [])


if __name__ == "__main__":
    unittest.main()
