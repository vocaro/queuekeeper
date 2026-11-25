"""The release script (tools/release.py, vocaro/queuekeeper#348): every version reference agrees, and a release is
bumped and published in two steps that refuse what they should, run against a temporary repository with a stand-in gh."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/release.py"


def loaded():
    spec = importlib.util.spec_from_file_location("release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = loaded()

NOTES = """Queuekeeper is a merge queue.

### Install

```sh
git clone --branch v1.0.0 https://github.com/vocaro/queuekeeper.git
```

Follow the [guide](https://github.com/vocaro/queuekeeper/blob/v1.0.0/README.md#getting-started).

## Thanks

To everyone.
"""


def write(root, path, text):
    file = Path(root) / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def pyproject(name, version, *pins):
    dependencies = ", ".join(f'"{pin}"' for pin in pins)
    return (f'[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "{name}"\nversion = "{version}"\n'
            f'requires-python = ">=3.14"\ndependencies = [{dependencies}]\n')


def tree(root, version="1.0.0"):
    """A checkout with each kind of reference the real one has, all reading version."""
    write(root, "pyproject.toml", pyproject("queuekeeper", version))
    write(root, "src/python/queuekeeper/__init__.py", f'"""Queuekeeper."""\n\n__version__ = "{version}"\n')
    write(root, "plugins/mcp/src/queuekeeper_mcp/server.py",
          f'NAME = "queuekeeper"\nVERSION = "{version}"\nPROTOCOL_VERSIONS = ("2025-06-18",)\n')
    write(root, "plugins/mcp/pyproject.toml", pyproject("queuekeeper-mcp", version, f"queuekeeper=={version}"))
    write(root, "plugins/mobile/pyproject.toml", pyproject("queuekeeper-mobile", version, f"queuekeeper=={version}"))
    write(root, "plugins/apple/pyproject.toml", pyproject("queuekeeper-apple", version, f"queuekeeper=={version}",
                                                          f"queuekeeper-mobile=={version}"))
    write(root, "plugins/apple/src/queuekeeper_apple/shards.py", 'EVENT_STREAM_VERSION = "0"\n')


class VersionsAgree(unittest.TestCase):
    def test_every_version_reference_in_this_checkout_agrees(self):
        found, problems = release.survey(release.WorkingTree(ROOT))
        self.assertEqual(problems, [])
        files = {reference.path for reference in found}
        self.assertLessEqual({"pyproject.toml", *release.CONSTANTS}, files)
        # Every plugin is covered, by its version and its pin on the core.
        for plugin in (ROOT / "plugins").iterdir():
            if not plugin.is_dir(): continue
            with self.subTest(plugin=plugin.name):
                labels = {r.label for r in found if r.path == f"plugins/{plugin.name}/pyproject.toml"}
                self.assertLessEqual({"version", "queuekeeper=="}, labels)
        import queuekeeper
        self.assertEqual(found[0].value, queuekeeper.__version__)

    def test_each_disagreement_and_each_uncovered_plugin_or_constant_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree(root)
            self.assertEqual(release.survey(release.WorkingTree(root))[1], [])
            write(root, "plugins/mobile/pyproject.toml",
                  pyproject("queuekeeper-mobile", "1.0.1", "queuekeeper==1.0.0"))
            write(root, "plugins/apple/pyproject.toml",
                  pyproject("queuekeeper-apple", "1.0.0", "queuekeeper==0.9.0", "queuekeeper-mobile>=1.0.0"))
            write(root, "plugins/mcp/src/queuekeeper_mcp/server.py", 'VERSION = "1.1.0"\n')
            write(root, "plugins/stray/README.md", "No pyproject.\n")
            write(root, "plugins/loose/pyproject.toml", pyproject("queuekeeper-loose", "1.0.0", "queuekeeper-gone==1.0.0"))
            write(root, "plugins/loose/src/queuekeeper_loose/__init__.py", '__version__ = "1.0.0"\n')
            found, problems = release.survey(release.WorkingTree(root))
            self.assertEqual(problems, [
                "plugins/apple/pyproject.toml: 'queuekeeper-mobile>=1.0.0' is not an exact == pin",
                "plugins/loose/pyproject.toml does not pin the core (queuekeeper==)",
                "plugins/stray has no pyproject.toml, so its version is not covered",
                "plugins/loose/pyproject.toml:9: pins queuekeeper-gone, which is no package here",
                "plugins/loose/src/queuekeeper_loose/__init__.py:1: __version__ is a version constant tools/release.py "
                "does not rewrite; add it to CONSTANTS",
                "plugins/mcp/src/queuekeeper_mcp/server.py:1: VERSION reads 1.1.0, not 1.0.0",
                "plugins/apple/pyproject.toml:9: queuekeeper== reads 0.9.0, not 1.0.0",
                "plugins/mobile/pyproject.toml:7: version reads 1.0.1, not 1.0.0"])

    def test_the_check_step_exits_1_on_a_disagreement(self):
        with tempfile.TemporaryDirectory() as directory:
            tree(directory)
            out = io.StringIO()
            self.assertEqual(release.main(["check"], root=Path(directory), out=out), 0)
            self.assertIn("plugins/apple/pyproject.toml:9: queuekeeper-mobile==1.0.0", out.getvalue())
            write(directory, "pyproject.toml", pyproject("queuekeeper", "2.0.0"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(release.main(["check"], root=Path(directory), out=io.StringIO()), 1)


class Repository(unittest.TestCase):
    """A clone of a bare origin whose main holds tree() at 1.0.0, tagged v1.0.0, and two commits after the tag."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        stubs = self.base / "bin"
        stubs.mkdir()
        self.calls = self.base / "gh-calls.jsonl"
        host_isolation.executable(stubs / "gh", f"""#!{sys.executable}
import json, sys
with open({str(self.calls)!r}, "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["release", "view"]: print({NOTES!r})
""")
        self.enterContext(patch.dict(os.environ, {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}))
        self.origin, self.repo = self.base / "origin.git", self.base / "repo"
        self.git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.base, "clone", "-q", str(self.origin), str(self.repo))
        tree(self.repo)
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "Start")
        self.git(self.repo, "tag", "-a", "v1.0.0", "-m", "Queuekeeper 1.0.0")
        for subject in ("Add a lane", "Fix a wait"):
            write(self.repo, "notes.txt", subject)
            self.git(self.repo, "add", ".")
            self.git(self.repo, "commit", "-qm", subject)
        self.git(self.repo, "push", "-q", "origin", "HEAD:main", "v1.0.0")
        self.git(self.repo, "fetch", "-q", "origin")

    def git(self, cwd, *words):
        return subprocess.run(["git", *words], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def step(self, *argv):
        """Run a step; its exit status, standard output and standard error."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            status = release.main(list(argv), root=self.repo, out=out)
        return status, out.getvalue(), err.getvalue()

    def refused(self, reason, *argv):
        head = self.git(self.repo, "rev-parse", "HEAD")
        status, _, err = self.step(*argv)
        self.assertEqual(status, 1, err)
        self.assertIn(reason, err)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), head)
        return err

    def gh(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def land(self):
        """Bump to 1.1.0 and push the bump to origin's main, as the queue would."""
        self.assertEqual(self.step("bump", "1.1.0")[0], 0)
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")


class Bump(Repository):
    def test_it_rewrites_every_reference_and_commits_on_a_release_branch(self):
        status, out, err = self.step("bump", "1.1.0")
        self.assertEqual(status, 0, err)
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "release-1.1.0")
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%B"), "Release Queuekeeper 1.1.0\n\nRefs #348")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        found, problems = release.survey(release.WorkingTree(self.repo))
        self.assertEqual(problems, [])
        self.assertEqual({r.value for r in found}, {"1.1.0"})
        self.assertEqual(len(found), 10)
        self.assertIn('EVENT_STREAM_VERSION = "0"', (self.repo / "plugins/apple/src/queuekeeper_apple/shards.py").read_text())
        self.assertIn("python3 scripts/qk land --wait --json", out)
        # It lands nothing by itself.
        self.assertEqual(self.git(self.repo, "rev-parse", "origin/main"), self.git(self.repo, "rev-parse", "main"))
        self.assertEqual(self.gh(), [])

    def test_it_refuses_a_tree_with_a_change_or_an_untracked_file(self):
        write(self.repo, "notes.txt", "edited")
        self.refused("the tree has changes", "bump", "1.1.0")
        self.git(self.repo, "checkout", "notes.txt")
        write(self.repo, "new.txt", "untracked")
        self.refused("the tree has changes", "bump", "1.1.0")

    def test_it_refuses_a_head_other_than_current_origin_main(self):
        write(self.repo, "notes.txt", "local")
        self.git(self.repo, "commit", "-qam", "Local")
        self.refused("is not origin/main", "bump", "1.1.0")
        # Behind: another clone moved origin's main, which the bump fetches.
        self.git(self.repo, "reset", "-q", "--hard", "origin/main")
        other = self.base / "other"
        self.git(self.base, "clone", "-q", str(self.origin), str(other))
        write(other, "notes.txt", "elsewhere")
        self.git(other, "commit", "-qam", "Elsewhere")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        self.refused("is not origin/main", "bump", "1.1.0")

    def test_it_refuses_a_version_that_is_not_greater_or_not_x_y_z(self):
        self.refused("1.0.0 is not greater than the current version 1.0.0", "bump", "1.0.0")
        self.refused("0.9.9 is not greater", "bump", "0.9.9")
        self.refused("is not a version of the form X.Y.Z", "bump", "1.1")
        self.refused("is not a version of the form X.Y.Z", "bump", "v1.1.0")
        self.assertEqual(self.git(self.repo, "branch", "--list", "release-*"), "")

    def test_it_refuses_references_that_already_disagree(self):
        write(self.repo, "plugins/mobile/pyproject.toml", pyproject("queuekeeper-mobile", "1.0.0", "queuekeeper==0.9.0"))
        self.git(self.repo, "commit", "-qam", "Stray pin")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")
        err = self.refused("the version references disagree", "bump", "1.1.0")
        self.assertIn("plugins/mobile/pyproject.toml:9: queuekeeper== reads 0.9.0, not 1.0.0", err)

    def test_it_refuses_a_release_branch_that_exists(self):
        self.git(self.repo, "branch", "release-1.1.0")
        self.refused("branch release-1.1.0 already exists", "bump", "1.1.0")


class Publish(Repository):
    def setUp(self):
        super().setUp()
        self.notes = self.base / "notes.md"

    def test_it_refuses_until_origin_main_holds_the_bump(self):
        err = self.refused("origin/main", "publish", "1.1.0", "--notes", str(self.notes))
        self.assertIn("does not hold the bump to 1.1.0", err)
        self.assertIn("pyproject.toml:7: version = 1.0.0, not 1.1.0", err)
        # A bump committed but not landed is not enough.
        self.assertEqual(self.step("bump", "1.1.0")[0], 0)
        self.refused("does not hold the bump", "publish", "1.1.0", "--notes", str(self.notes))
        self.assertFalse(self.notes.exists())
        self.assertEqual(self.gh(), [])

    def test_a_dry_run_drafts_the_notes_and_prints_the_commands_without_running_them(self):
        self.land()
        main = self.git(self.repo, "rev-parse", "origin/main")
        status, out, err = self.step("publish", "1.1.0", "--notes", str(self.notes))
        self.assertEqual(status, 0, err)
        self.assertIn(f"  git tag -a v1.1.0 -m 'Queuekeeper 1.1.0' {main}\n", out)
        self.assertIn("  git push origin v1.1.0\n", out)
        self.assertIn(f"  gh release create v1.1.0 --verify-tag --title 'Queuekeeper 1.1.0' --notes-file {self.notes}\n",
                      out)
        self.assertEqual(self.notes.read_text(), """### Install

```sh
git clone --branch v1.1.0 https://github.com/vocaro/queuekeeper.git
```

Follow the [guide](https://github.com/vocaro/queuekeeper/blob/v1.1.0/README.md#getting-started).

### Changes since v1.0.0

- Release Queuekeeper 1.1.0
- Fix a wait
- Add a lane
""")
        self.assertEqual(self.gh(), [["release", "view", "v1.0.0", "--json", "body", "--jq", ".body"]])
        self.assertEqual(self.git(self.repo, "tag", "--list", "v1.1.0"), "")
        self.assertEqual(self.git(self.repo, "ls-remote", "--tags", "origin", "v1.1.0"), "")

    def test_with_yes_it_tags_the_landed_commit_pushes_the_tag_and_creates_the_release(self):
        self.land()
        main = self.git(self.repo, "rev-parse", "origin/main")
        self.notes.write_text("Edited after the dry run.\n")
        status, out, err = self.step("publish", "1.1.0", "--notes", str(self.notes), "--yes")
        self.assertEqual(status, 0, err)
        self.assertIn("Keeping the release notes already at", out)
        self.assertEqual(self.notes.read_text(), "Edited after the dry run.\n")
        self.assertEqual(self.git(self.repo, "cat-file", "-t", "v1.1.0"), "tag")
        self.assertEqual(self.git(self.repo, "rev-parse", "v1.1.0^{commit}"), main)
        self.assertEqual(self.git(self.repo, "tag", "-l", "--format=%(contents:subject)", "v1.1.0"), "Queuekeeper 1.1.0")
        self.assertIn("refs/tags/v1.1.0", self.git(self.repo, "ls-remote", "--tags", "origin", "v1.1.0"))
        self.assertEqual(self.gh(), [["release", "create", "v1.1.0", "--verify-tag", "--title", "Queuekeeper 1.1.0",
                                      "--notes-file", str(self.notes)]])
        # Once tagged, it refuses the same release again.
        self.refused("tag v1.1.0 already exists", "publish", "1.1.0", "--notes", str(self.notes), "--yes")
        self.assertEqual(len(self.gh()), 1)

    def test_it_refuses_a_tag_that_exists_on_origin_only(self):
        self.land()
        other = self.base / "other"
        self.git(self.base, "clone", "-q", str(self.origin), str(other))
        self.git(other, "tag", "-a", "v1.1.0", "-m", "Elsewhere")
        self.git(other, "push", "-q", "origin", "v1.1.0")
        self.refused("tag v1.1.0 already exists", "publish", "1.1.0", "--notes", str(self.notes))

    def test_it_refuses_a_version_other_than_the_landed_one_or_not_x_y_z(self):
        self.land()
        self.refused("does not hold the bump to 1.2.0", "publish", "1.2.0", "--notes", str(self.notes))
        self.refused("is not a version of the form X.Y.Z", "publish", "1.1", "--notes", str(self.notes))
        self.assertEqual(self.gh(), [])


if __name__ == "__main__":
    unittest.main()
