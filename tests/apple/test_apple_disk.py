import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
from queuekeeper import cli, disk
from queuekeeper_apple import devices, disk as apple_disk
from queuekeeper_mac_desktop import reservations
import host_isolation

apple_disk.register()
LATER = time.time() + 10 * 86400  # every fixture looks idle from here

def git(cwd, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test",
                       GIT_AUTHOR_EMAIL="test@example.com", GIT_COMMITTER_NAME="Test",
                       GIT_COMMITTER_EMAIL="test@example.com")
    return subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True,
                          env=environment).stdout.strip()


def commit(cwd, name, text):
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.root)])
        self.home = self.root / "home"
        self.development = self.home / "Development"
        self.main = self.development / "Project"
        self.main.mkdir(parents=True)
        git(self.main, "init", "-q", "-b", "main")
        commit(self.main, "README", "first\n")
        self.publish()

    def publish(self):
        """Point origin/main at the main checkout's HEAD, as a fetch would."""
        git(self.main, "update-ref", "refs/remotes/origin/main", "HEAD")

    def worktree(self, name, parent=None, branch=None):
        path = (parent or self.main / ".claude/worktrees") / name
        git(self.main, "worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), str(path), "origin/main")
        return path

    def host(self, processes=(), now=LATER, **values):
        return apple_disk.Host(home=self.home, development=self.development, now=now, min_idle=86400,
                         processes=list(processes), **values)

    def items(self, host, categories=None):
        return {item.path: item for item in disk.inventory(host, disk.Sizes(measure=False), categories)}


def device(name, state="Shutdown", used="2026-01-01T00:00:00Z", **extra):
    return {"udid": str(uuid.uuid4()).upper(), "name": name, "state": state, "isAvailable": True,
            "lastUsedAt": used, **extra}


class SimulatorTests(Fixture):
    def simulators(self, devices, **values):
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": devices}}
        items = disk.inventory(self.host(simulators=listing, **values), disk.Sizes(measure=False), ["simulator"])
        return {item.path.split(" ")[0]: item for item in items}

    def test_an_abandoned_disposable_device_is_a_candidate_and_a_pool_device_never_is(self):
        disposable = device(f"Project-iPhone-{uuid.uuid4()}")
        pool = device("Project Duo 1")
        unavailable_pool = device("Project Duo 2", isAvailable=False, availabilityError="runtime missing")
        default = device("iPhone 17 Pro")
        items = self.simulators([disposable, pool, unavailable_pool, default],
                                default_devices=frozenset({default["udid"]}))
        self.assertEqual(items[disposable["udid"]].status, disk.CANDIDATE, items[disposable["udid"]].reason)
        self.assertEqual(items[disposable["udid"]].command, ["xcrun", "simctl", "delete", disposable["udid"]])
        self.assertEqual(items[pool["udid"]].status, disk.KEPT)
        self.assertIsNone(items[pool["udid"]].command)
        # Xcode makes its defaults again, and nothing runs on a device whose runtime is gone (the owner's
        # decision, 2026-11-03).
        self.assertEqual(items[unavailable_pool["udid"]].status, disk.CANDIDATE)
        self.assertEqual(items[default["udid"]].status, disk.CANDIDATE)
        self.assertEqual(items[default["udid"]].command, ["xcrun", "simctl", "delete", default["udid"]])

    def test_a_pool_template_is_kept_until_its_runtime_is_gone(self):
        template = device("Project Duo template (iOS-27-0)")
        gone = device("Project iPad template (iOS-27-0)", isAvailable=False, availabilityError="runtime missing")
        # The same name in another pool, or on another runtime than its name says, is no template of this pool.
        stranger = device("Other Duo template (iOS-27-0)")
        mislabeled = device("Project Duo template (iOS-26-0)")
        record = json.dumps({"devices": {"Project Duo template (iOS-27-0)": {"device": template["udid"],
                                                                             "state": "released"}}})
        items = self.simulators([template, gone, stranger, mislabeled], pools=frozenset({"Project"}),
                                records={"/record.json": record})
        self.assertEqual(items[template["udid"]].status, disk.KEPT)
        self.assertIn("the Project pool's warmed template", items[template["udid"]].reason)
        self.assertIsNone(items[template["udid"]].command)
        self.assertEqual(items[gone["udid"]].status, disk.CANDIDATE)
        self.assertIn("its runtime gone; the next new member of the kind warms another", items[gone["udid"]].reason)
        # Its deletion goes through the pool's prune, which reserves it, checks it again and records it.
        self.assertEqual(items[gone["udid"]].command[-6:],
                         ["simulator-pool", "--pool", "Project", "--owner", "qk disk", "prune", gone["udid"]][-6:])
        for other in (stranger, mislabeled):
            self.assertNotIn("warmed template", items[other["udid"]].reason)

    def test_a_pool_member_is_a_candidate_only_under_the_pruning_rule(self):
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        # vocaro-engineering#259: an hour after its use and release, any member but the lowest of its kind goes.
        lowest, stale, recent, released, booted = (device("Project Duo 1", used=stamp(0)),
            device("Project Duo 2", used=stamp(3 / 24)), device("Project Duo 3", used=stamp(0.5 / 24)),
            device("Project Duo 4", used=stamp(3 / 24)), device("Project Duo 5", state="Booted", used=stamp(20)))
        other = device("Other Duo 2", used=stamp(40))
        record = json.dumps({"devices": {"Project Duo 4": {"device": released["udid"], "state": "released",
                                                           "releasedAt": stamp(0.5 / 24)}}})
        items = self.simulators([lowest, stale, recent, released, booted, other], pools=frozenset({"Project"}),
                                records={"/record.json": record})
        self.assertEqual(items[stale["udid"]].status, disk.CANDIDATE, items[stale["udid"]].reason)
        self.assertEqual(items[stale["udid"]].command[-6:],
                         ["simulator-pool", "--pool", "Project", "--owner", "qk disk", "prune", stale["udid"]][-6:])
        self.assertIn("eligible for pruning", items[stale["udid"]].reason)
        for member, reason in ((lowest, "lowest-numbered"), (recent, "within 1 hour"), (released, "was released"),
                               (booted, "Booted")):
            self.assertEqual(items[member["udid"]].status, disk.KEPT, reason)
            self.assertIn(reason, items[member["udid"]].reason)
        # A device in no configured pool is the owner's, however old.
        self.assertEqual(items[other["udid"]].status, disk.KEPT)
        self.assertIn("in no project's pool", items[other["udid"]].reason)

    def test_the_pools_are_those_the_main_checkouts_name(self):
        (self.main / "queuekeeper.json").write_text(json.dumps({"simulatorPool": "Project"}))
        (self.development / "Unconfigured").mkdir()
        self.assertEqual(apple_disk.pools(self.development), frozenset({"Project"}))
        # A main checkout that lags origin/main still names the pool origin/main names.
        lagging = self.development / "Lagging"
        lagging.mkdir()
        git(lagging, "init", "-q", "-b", "main")
        commit(lagging, "queuekeeper.json", json.dumps({"simulatorPool": "Lagging"}))
        git(lagging, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(lagging, "rm", "-q", "queuekeeper.json")
        git(lagging, "commit", "-q", "-m", "older")
        self.assertEqual(apple_disk.pools(self.development), frozenset({"Project", "Lagging"}))

    def test_booted_recent_reserved_or_referenced_disposable_devices_are_kept(self):
        booted = device(f"P-{uuid.uuid4()}", state="Booted")
        recent = device(f"P-{uuid.uuid4()}", used=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - 600)))
        by_udid = device(f"P-{uuid.uuid4()}")
        by_name = device(f"P-{uuid.uuid4()}")
        argument = device(f"P-{uuid.uuid4()}")
        record = json.dumps({"devices": {"a": {"device": by_udid["udid"].lower()}, "b": {"device": by_name["name"]}}})
        process = disk.Process(9, "xcodebuild", args=f"xcodebuild test -destination platform=iOS Simulator,id={argument['udid']}")
        items = self.simulators([booted, recent, by_udid, by_name, argument], records={"/record.json": record},
                                processes=[process])
        expected = {booted["udid"]: "Booted", recent["udid"]: "last used 10 min ago",
                    by_udid["udid"]: "device record /record.json", by_name["udid"]: "device record /record.json",
                    argument["udid"]: "arguments of PID 9"}
        for udid, reason in expected.items():
            self.assertEqual(items[udid].status, disk.KEPT, reason)
            self.assertIn(reason, items[udid].reason)

    def test_a_disposable_device_named_only_in_the_record_at_the_override_is_kept(self):
        reserved, unnamed = device(f"P-{uuid.uuid4()}"), device(f"P-{uuid.uuid4()}")
        record = self.root / "isolated-record.json"
        record.write_text(json.dumps({"devices": {"a": {"device": reserved["udid"]}}}))
        with patch.dict(os.environ, {"QUEUEKEEPER_DEVICE_RECORD": str(record)}):
            items = self.simulators([reserved, unnamed], records=apple_disk.device_records())
        self.assertEqual(items[reserved["udid"]].status, disk.KEPT)
        self.assertIn(f"named in the device record {record}", items[reserved["udid"]].reason)
        self.assertEqual(items[unnamed["udid"]].status, disk.CANDIDATE, items[unnamed["udid"]].reason)

    def test_device_records_follow_the_devices_module_and_keep_the_legacy_records(self):
        # A record moved by the devices module, as on a host without ~/Library, and a legacy one.
        moved, legacy = self.root / "moved/device-handoff.json", self.root / "old-device-handoff.json"
        moved.parent.mkdir()
        for path in [moved, legacy]: path.write_text("{}")
        environment = {key: value for key, value in os.environ.items() if key != "QUEUEKEEPER_DEVICE_RECORD"}
        with patch.dict(os.environ, environment, clear=True), patch.object(devices, "default_path", return_value=moved), \
             patch.object(reservations, "default_path", return_value=moved), \
             patch.object(devices, "LEGACY", str(self.root / "*device-handoff*.json")):
            records = apple_disk.device_records()
        self.assertEqual(set(records) & {str(moved), str(legacy)}, {str(moved), str(legacy)})

    def test_an_unreadable_device_record_keeps_every_disposable_device(self):
        disposable = device(f"P-{uuid.uuid4()}")
        items = self.simulators([disposable], records={"/record.json": None})
        self.assertEqual(items[disposable["udid"]].status, disk.KEPT)
        self.assertIn("could not be read", items[disposable["udid"]].reason)

    def test_a_runtime_no_device_uses_is_a_candidate_once_unused_for_14_days(self):
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [device("iPhone 17")]}}
        runtime = lambda key, version, **values: {"identifier": key, "sizeBytes": 8,
            "runtimeIdentifier": f"com.apple.CoreSimulator.SimRuntime.iOS-{version}", **values}
        images = {"A": runtime("A", "27-0", lastUsedAt=stamp(40)), "B": runtime("B", "26-5"),
                  "C": runtime("C", "27-1", lastUsedAt=stamp(20)), "D": runtime("D", "27-2", lastUsedAt=stamp(3)),
                  "E": runtime("E", "27-3", deletable=False)}
        items = {item.path[0]: item for item in disk.inventory(self.host(simulators=listing, runtimes=images, now=LATER),
                                                                disk.Sizes(measure=False), ["runtime"])}
        self.assertEqual(items["A"].status, disk.KEPT)  # a device uses it, however long ago
        for key in "BC":
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertEqual(items[key].command, ["xcrun", "simctl", "runtime", "delete", key])
        self.assertEqual(items["D"].status, disk.KEPT)
        self.assertIn("within 14 days", items["D"].reason)
        self.assertEqual(items["E"].status, disk.KEPT)

    def test_a_superseded_runtime_build_is_a_candidate_unless_chosen_pinned_reserved_or_recent(self):
        # vocaro-engineering#188: devices of an identifier run on one build, so another build of it is idle.
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        ios = lambda version: f"com.apple.CoreSimulator.SimRuntime.iOS-{version}"
        listing = {"devices": {ios(version): [device("iPhone 17")] for version in ("27-0", "27-2", "27-3", "27-4")}}
        runtime = lambda key, version, build, **values: {"identifier": key, "sizeBytes": 8, "build": build,
                                                          "runtimeIdentifier": ios(version), **values}
        images = {"old": runtime("old", "27-2", "24B5000", lastUsedAt=stamp(30)),
                  "chosen": runtime("chosen", "27-2", "24B8117", lastUsedAt=stamp(40)),
                  "pinned": runtime("pinned", "27-2", "24B5110a", lastUsedAt=stamp(30)),
                  "beta": runtime("beta", "27-0", "24A5423a"),
                  "release": runtime("release", "27-0", "24A434", lastUsedAt=stamp(1)),
                  "recent": runtime("recent", "27-3", "24C10", lastUsedAt=stamp(6)),
                  "newer": runtime("newer", "27-3", "24C20", lastUsedAt=stamp(1)),
                  "reserved": runtime("reserved", "27-4", "24D10", lastUsedAt=stamp(30)),
                  "current": runtime("current", "27-4", "24D20", lastUsedAt=stamp(1)),
                  "only": runtime("only", "27-5", "24E10", lastUsedAt=stamp(30))}
        matches = {"iphoneos27.2": {"chosenRuntimeBuild": "24B8117", "sdkVersion": "27.2"}}
        pinned = {"24B5110a": "qualified for guarded VoiceOver activation", "24E10": "named by a test pin"}
        records = {"/record.json": json.dumps({"devices": {"x": {"runtimeBuild": "24D10"},
                   # A warmed template's census of installed builds names a build it does not run.
                   "y": {"template": {"runtimeBuild": "24A434", "installedBuilds": ["24A434", "24A5423a"]}}}})}
        def items(**values):
            host = self.host(simulators=listing, runtimes=images, matches=matches, pinned=pinned, records=records)
            for name, value in values.items(): setattr(host, name, value)
            return {item.path.split(" ")[0]: item for item in disk.inventory(host, disk.Sizes(measure=False), ["runtime"])}
        found = items()
        for key in ("old", "beta"):
            self.assertEqual(found[key].status, disk.CANDIDATE, found[key].reason)
            self.assertEqual(found[key].command, ["xcrun", "simctl", "runtime", "delete", key])
        self.assertIn("superseded by 24B8117", found["old"].reason)
        self.assertIn("iphoneos27.2 SDK", found["old"].reason)
        self.assertIn("superseded by 24A434", found["beta"].reason)
        self.assertIn("CoreSimulator used last", found["beta"].reason)
        expected = {"chosen": "devices use it", "release": "devices use it", "newer": "devices use it",
                    "current": "devices use it", "pinned": "qualified for guarded VoiceOver activation",
                    "recent": "within 14 days", "reserved": "the device record /record.json names it",
                    "only": "named by a test pin"}
        for key, why in expected.items():
            self.assertEqual(found[key].status, disk.KEPT, key)
            self.assertIn(why, found[key].reason, key)
            self.assertIsNone(found[key].command, key)
        # Without the SDK's choice, two builds CoreSimulator used last at once leave the choice unknown.
        self.assertEqual(items(matches=None)["old"].status, disk.KEPT)
        self.assertIn("devices use it", items(matches=None)["old"].reason)
        # A record that cannot be read keeps every superseded build.
        self.assertIn("could not be read", items(records={"/record.json": None})["old"].reason)
        # When neither the SDK nor any use shows the choice, no build of the identifier is superseded.
        unused = {key: {**image, "lastUsedAt": None} for key, image in images.items()}
        self.assertEqual(items(runtimes=unused, matches=None)["beta"].status, disk.KEPT)


    def test_a_runtime_candidate_names_the_second_macs_copy_and_stays_while_that_copy_would_come_back(self):
        # vocaro-engineering#188 and #123: `qk remote setup --runtimes` keeps both Macs' runtimes alike.
        stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATER - days * 86400))
        ios = lambda version: f"com.apple.CoreSimulator.SimRuntime.iOS-{version}"
        listing = {"devices": {ios("27-0"): [device("iPhone 17")]}}
        images = {"beta": {"identifier": "beta", "sizeBytes": 8, "build": "24A5423a", "runtimeIdentifier": ios("27-0")},
                  "release": {"identifier": "release", "sizeBytes": 8, "build": "24A434", "runtimeIdentifier": ios("27-0"),
                              "lastUsedAt": stamp(1)},
                  "unused": {"identifier": "unused", "sizeBytes": 8, "build": "24A100", "runtimeIdentifier": ios("26-0")}}
        def found(there, second="build-host"):
            host = self.host(simulators=listing, runtimes=images, offload_host=second, remote_runtimes=there)
            return {item.path.split(" ")[0]: item for item in disk.inventory(host, disk.Sizes(measure=False), ["runtime"])}
        copy = lambda build, users: {"build": build, "users": users}
        items = found({"images": [copy("24A434", ["A"])]})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertIn("build-host holds no copy, so removing it here also leaves the two Macs alike", items[key].reason)
        items = found({"images": [copy("24A5423a", []), copy("24A100", [])]})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.CANDIDATE, items[key].reason)
            self.assertIn("build-host holds a copy no simulator uses there", items[key].reason)
        items = found({"images": [copy("24A5423a", ["A", "B"]), copy("24A100", None)]})
        self.assertEqual(items["beta"].status, disk.KEPT)
        self.assertIn("superseded by 24A434", items["beta"].reason)
        self.assertIn("the 2 simulators on its identifier there count as used, so `qk remote setup --runtimes` "
                      "would copy it back here", items["beta"].reason)
        self.assertEqual(items["unused"].status, disk.KEPT)
        self.assertIn("could not be listed", items["unused"].reason)
        items = found({"error": "it did not answer in 180s"})
        for key in ("beta", "unused"):
            self.assertEqual(items[key].status, disk.KEPT)
            self.assertIsNone(items[key].command)
            self.assertIn("build-host's runtimes could not be read (it did not answer in 180s)", items[key].reason)
        self.assertEqual(items["release"].status, disk.KEPT)  # the chosen build is untouched
        # Without a second Mac the reason names none.
        items = found(None, second=None)
        self.assertEqual(items["beta"].status, disk.CANDIDATE)
        self.assertNotIn("build-host", items["beta"].reason)

    def test_the_second_macs_runtimes_are_read_as_eng_remote_reads_them(self):
        from queuekeeper import remote
        self.assertIsNone(apple_disk.runtimes_there(None))
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", return_value={"images": [{"build": "B", "users": []}]}) as read:
            self.assertEqual(apple_disk.runtimes_there("build-host"), {"images": [{"build": "B", "users": []}]})
        read.assert_called_once_with("build-host", "build-host", "/X.app")
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", side_effect=remote.Misconfigured("unreachable", "fix")):
            self.assertIn("error", apple_disk.runtimes_there("build-host"))
        with patch.object(remote, "developer_directory", return_value="/X.app"), \
                patch.object(remote, "runtime_images", return_value={"images": None}):
            self.assertEqual(apple_disk.runtimes_there("build-host"), {"error": "simctl did not list its runtime images"})


class CacheTests(Fixture):
    def derived(self, name, workspace=None):
        directory = self.home / "Library/Developer/Xcode/DerivedData" / name
        (directory / "Build").mkdir(parents=True)
        if workspace:
            with open(directory / "info.plist", "wb") as handle: plistlib.dump({"WorkspacePath": str(workspace)}, handle)
        return directory

    def test_derived_data_is_a_candidate_only_when_its_workspace_is_gone_and_nothing_uses_it(self):
        present = self.derived("Present-" + "a" * 28, self.main)
        gone = self.derived("Gone-" + "b" * 28, self.main / ".claude/worktrees/removed/Project.xcodeproj")
        used = self.derived("Used-" + "c" * 28, self.main / ".claude/worktrees/removed/Other.xcodeproj")
        unknown = self.derived("Unknown-" + "d" * 28)
        shared = self.derived("ModuleCache.noindex")
        explicit = self.derived("SDKExplicitPrecompiledModules")
        session = self.derived("Session-" + "e" * 28, self.root / "tmp/claude-501/session/App.xcodeproj")
        process = disk.Process(5, "XCBBuildService", paths=(str(used / "Build/Products/x.o"),))
        items = self.items(self.host([process], temp_roots=[(self.root / "tmp", 6)]), ["derived-data"])
        self.assertEqual(items[str(session)].status, disk.CANDIDATE)
        self.assertEqual(items[str(gone)].status, disk.CANDIDATE)
        self.assertEqual(items[str(gone)].command, ["rm", "-rf", "--", str(gone)])
        self.assertEqual(items[str(present)].status, disk.KEPT)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertEqual(items[str(unknown)].status, disk.OWNER)
        self.assertIn("holds Build", items[str(unknown)].reason)
        self.assertEqual(items[str(shared)].status, disk.KEPT)
        self.assertEqual(items[str(explicit)].status, disk.KEPT)

    def test_derived_data_recording_no_workspace_is_a_candidate_only_without_build_products(self):
        root = self.home / "Library/Developer/Xcode/DerivedData"
        def tree(name, *folders):
            directory = root / (name + "-" + "f" * 28)
            for folder in folders: (directory / folder).mkdir(parents=True)
            return directory
        resolved = tree("Resolved", "Logs", "SourcePackages", "TestResults")
        logs = tree("Logs", "Logs")
        built = tree("Built", "Logs", "SourcePackages", "Build")
        # What a test-without-building with no -derivedDataPath leaves: a test run's coverage data, no products.
        coverage = tree("Coverage", "Logs", "TestResults", "Build/ProfileData/F49BFB5B")
        (coverage / "Build/ProfileData/F49BFB5B/Coverage.profdata").write_bytes(b"profile")
        products = tree("Products", "Logs", "TestResults", "Build/ProfileData", "Build/Products")
        indexed = tree("Indexed", "Logs", "Index.noindex")
        used = tree("Used", "Logs", "SourcePackages")
        recent = tree("Recent", "Logs", "SourcePackages")
        os.utime(recent / "Logs", (LATER - 3600, LATER - 3600))
        process = disk.Process(6, "xcodebuild", cwd=str(used / "SourcePackages/checkouts/Package"))
        items = self.items(self.host([process]), ["derived-data"])
        for candidate in (resolved, logs, coverage):
            self.assertEqual(items[str(candidate)].status, disk.CANDIDATE, items[str(candidate)].reason)
            self.assertEqual(items[str(candidate)].command, ["rm", "-rf", "--", str(candidate)])
        self.assertIn("records no workspace; holds only Logs, SourcePackages, TestResults, no build products",
                      items[str(resolved)].reason)
        # A folder with build products, or anything else this rule does not know, stays the owner's decision.
        self.assertIn("records no workspace; holds only Build, Logs, TestResults, Build holding only coverage data",
                      items[str(coverage)].reason)
        for owner in (built, indexed, products):
            self.assertEqual(items[str(owner)].status, disk.OWNER, items[str(owner)].reason)
            self.assertIn("records no workspace; holds", items[str(owner)].reason)
        self.assertEqual(items[str(used)].status, disk.KEPT)
        self.assertIn("in use: PID 6 (xcodebuild)", items[str(used)].reason)
        self.assertEqual(items[str(recent)].status, disk.KEPT)
        self.assertIn("but changed 60 min ago", items[str(recent)].reason)

    def test_derived_data_of_a_workspace_outside_the_projects_is_not_listed(self):
        mine = self.derived("Mine-" + "a" * 28, self.root / "Desktop/Personal.xcodeproj")
        gone = self.derived("Gone-" + "b" * 28, self.root / "removed/Personal.xcodeproj")
        project = self.derived("Project-" + "c" * 28, self.development / "Removed/Project.xcodeproj")
        items = self.items(self.host(temp_roots=[(self.root / "tmp", 6)]), ["derived-data"])
        self.assertNotIn(str(mine), items)
        self.assertNotIn(str(gone), items)
        self.assertEqual(items[str(project)].status, disk.CANDIDATE)


class DeletionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.ledger = self.root / "ledger.jsonl"

    def test_simulator_deletion_names_the_exact_device(self):
        disposable, pool = device(f"P-{uuid.uuid4()}"), device("Project Duo 1")
        listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [disposable, pool]}}
        collect = lambda categories=None: disk.inventory(self.host(simulators=listing), disk.Sizes(measure=False), categories)
        calls = []
        def run(command, **options):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        with contextlib.redirect_stdout(StringIO()):
            disk.delete(collect(["simulator"]), lambda category: collect([category]), self.ledger, run=run)
        self.assertEqual(calls, [["xcrun", "simctl", "delete", disposable["udid"]]])


if __name__ == "__main__":
    unittest.main()
