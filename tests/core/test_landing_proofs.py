"""The proofs that a commit's work is in a base branch, against throwaway repositories.

The cases for a negated closing claim, and for the merges a branch brings in, are in test_merge_queue.py."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import landing_proofs


class Fixture(unittest.TestCase):
    """A bare repository that stands in for the remote, and a clone of it with one commit on main."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        for directory in (self.home, self.root / "tmp"): directory.mkdir()
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home), "LC_ALL": "C",
                    "TMPDIR": str(self.root / "tmp"), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com"}
        self.origin = self.root / "origin.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.main = self.home / "Development/Project"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.main))
        self.git(self.main, "symbolic-ref", "HEAD", "refs/heads/main")
        for name, text in (("README", "first\n"), ("shared.txt", "one\n"), ("CHANGELOG", "start\n")):
            (self.main / name).write_text(text)
        self.git(self.main, "add", ".")
        self.git(self.main, "commit", "-q", "-m", "First")
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.git(self.main, "remote", "set-head", "origin", "main")

    def git(self, cwd, *arguments):
        return subprocess.run(["git", "-C", str(cwd), *arguments], check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def commit(self, cwd, name, text, message=None):
        path = Path(cwd) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git(cwd, "add", name)
        self.git(cwd, "commit", "-q", "-m", message or f"Change {name}")
        return self.git(cwd, "rev-parse", "HEAD")

    def worktree(self, name):
        path = self.root / "worktrees" / name
        self.git(self.main, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        return path

    def publish(self):
        """Push the main checkout's main, as landing work on the remote does."""
        self.git(self.main, "push", "-q", "origin", "main")

    def run_git(self, *arguments):
        """git in the main checkout, as the proofs call it: its CompletedProcess, whatever its status."""
        return subprocess.run(["git", "-C", str(self.main), *arguments], capture_output=True, text=True, env=self.env)

    def proof(self, base, tip):
        return landing_proofs.proof(self.run_git, base, tip)

    def contained(self, base, tip):
        return landing_proofs.contained(self.run_git, base, tip)


class ContainedTests(Fixture):
    def test_each_proof_and_its_negative_control(self):
        first = self.git(self.main, "rev-parse", "HEAD")
        self.assertEqual(self.contained("main", first), landing_proofs.ANCESTOR)
        self.git(self.main, "checkout", "-q", "-b", "work")
        one = self.commit(self.main, "one.txt", "one\n")
        two = self.commit(self.main, "two.txt", "two\n")
        self.git(self.main, "checkout", "-q", "main")
        self.assertIsNone(self.contained("main", two))
        self.commit(self.main, "other.txt", "other\n")
        self.git(self.main, "cherry-pick", one, two)
        self.assertEqual(self.contained("main", two), landing_proofs.PATCH_EQUIVALENT)
        self.git(self.main, "reset", "-q", "--hard", "HEAD~2")
        self.git(self.main, "merge", "-q", "--squash", "work")
        self.git(self.main, "commit", "-q", "-m", "Squash")
        self.assertEqual(self.contained("main", two), landing_proofs.CONTENT_MERGED)
        self.commit(self.main, "one.txt", "edited over\n")
        self.assertIsNone(self.contained("main", two))
        self.assertIsNone(self.contained("main", "no-such-commit"))

    def test_a_merge_on_the_branch_rules_out_the_patch_proof(self):
        self.git(self.main, "checkout", "-q", "-b", "work")
        self.commit(self.main, "one.txt", "one\n")
        self.git(self.main, "checkout", "-q", "-b", "side", "main")
        self.commit(self.main, "side.txt", "side\n")
        self.git(self.main, "checkout", "-q", "work")
        self.git(self.main, "merge", "-q", "--no-ff", "--no-edit", "side")
        (self.main / "evil.txt").write_text("a change the merge itself makes\n")
        self.git(self.main, "add", "evil.txt")
        self.git(self.main, "commit", "-q", "--amend", "--no-edit")
        tip = self.git(self.main, "rev-parse", "HEAD")
        self.git(self.main, "checkout", "-q", "main")
        self.git(self.main, "cherry-pick", "work~1", "side")
        self.assertIsNone(self.contained("main", tip))  # every patch is there, but not the merge's own


CHANGELOG = "# Changelog\n\n## Unreleased\n\n- Old entry\n"
DOC = "# Testing\n\n## Lanes\n\nThe lanes run in parallel.\n\n## Devices\n\nEach lane takes a device.\n"


def changelog(*entries):
    """CHANGELOG with these lines at the top of its Unreleased section."""
    return CHANGELOG.replace("## Unreleased\n\n", "## Unreleased\n\n" + "".join(f"{entry}\n" for entry in entries))


class ResolvedTests(Fixture):
    """The merge queue rebases an entry past those ahead of it and settles a changelog conflict by
    keeping both sides, so the commit it lands adds the branch's very lines beside other neighbors
    and its patch is not the branch's (vocaro/vocaro-engineering#164). Each branch commit then counts
    as landed when a commit on main with its subject and author adds and removes exactly its lines,
    and differs only in the lines around them; anything a resolution dropped or changed proves nothing."""

    def setUp(self):
        super().setUp()
        self.commit(self.main, "CHANGELOG.md", CHANGELOG, "Start a changelog")
        self.commit(self.main, "doc/testing.md", DOC, "Start the testing guide")
        self.publish()

    def ahead(self, doc=None):
        """An entry the queue landed first, adding its own line where the branch adds one, and with
        doc, editing the guide near where the branch does."""
        self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
        self.commit(self.main, "CHANGELOG.md", changelog("- Theirs"), "Land the entry ahead")
        if doc: self.commit(self.main, "doc/testing.md", doc((self.main / "doc/testing.md").read_text()), "Edit the guide")
        self.publish()

    def land_as(self, source, files):
        """Land source on main as the queue does: these files' contents, with its message and author."""
        self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
        for name, text in files.items():
            path = self.main / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.git(self.main, "add", str(path))
        self.git(self.main, "commit", "-q", "-C", source)
        self.publish()
        return self.git(self.main, "rev-parse", "HEAD")

    def entry(self):
        """A worktree on branch entry with one commit that adds code and a changelog line."""
        path = self.worktree("entry")
        (path / "code.py").write_text("print('mine')\n")
        (path / "CHANGELOG.md").write_text(changelog("- Mine"))
        self.git(path, "add", "code.py", "CHANGELOG.md")
        self.git(path, "commit", "-q", "-m", "Add mine")
        return path, self.git(path, "rev-parse", "HEAD")

    def test_a_changelog_kept_both_sides_proves_it_landed(self):
        path, tip = self.entry()
        self.ahead()
        landed = self.land_as(tip, {"code.py": "print('mine')\n", "CHANGELOG.md": changelog("- Mine", "- Theirs")})
        self.assertEqual(self.proof("origin/main", tip),
                         (landing_proofs.RESOLVED, f"{tip[:12]} as {landed[:12]}, whose patch differs only in where "
                                                   "its lines sit in CHANGELOG.md"))
        self.assertIsNone(self.contained("origin/main~1", tip))

    def test_a_resolution_that_dropped_or_changed_a_line_or_changed_another_file_proves_nothing(self):
        path, tip = self.entry()
        self.ahead()
        for files in ({"code.py": "print('mine')\n"},  # the queue kept only the entry ahead
                      {"code.py": "print('mine')\n",  # it reworded the branch's line
                       "CHANGELOG.md": changelog("- Mine, reworded", "- Theirs")},
                      {"code.py": "print('changed')\n",  # it changed the code beside the kept line
                       "CHANGELOG.md": changelog("- Mine", "- Theirs")}):
            with self.subTest(files=files):
                self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
                before = self.git(self.main, "rev-parse", "HEAD")
                self.land_as(tip, files)
                self.assertEqual(self.proof("origin/main", tip), (None, ""))
                self.git(self.main, "reset", "-q", "--hard", before)
                self.git(self.main, "push", "-q", "--force", "origin", "main")

    def test_a_commit_with_another_subject_or_author_is_not_the_branchs_landing(self):
        path, tip = self.entry()
        self.ahead()
        (self.main / "code.py").write_text("print('mine')\n")
        (self.main / "CHANGELOG.md").write_text(changelog("- Mine", "- Theirs"))
        self.git(self.main, "add", "code.py", "CHANGELOG.md")
        self.git(self.main, "commit", "-q", "--author", "Other <other@example.com>", "-m", "Add mine")
        self.publish()
        self.assertIsNone(self.contained("origin/main", tip))
        self.git(self.main, "commit", "-q", "--amend", "--reset-author", "-m", "Add something else")
        self.git(self.main, "push", "-q", "--force", "origin", "main")
        self.assertIsNone(self.contained("origin/main", tip))

    def test_stacked_commits_landed_apart_stay_landed_after_main_rewrites_their_lines(self):
        """EPUBLib's five branches of 2026-11-04 (vocaro/vocaro-engineering#164): two commits on a stack,
        each landed with a changelog kept both sides and the guide's context changed ahead of it, and then
        main rewrote the guide's line both of them wrote. Neither added line is in main's tree any more,
        and each commit is still landed, since its own landed commit adds it."""
        path = self.worktree("lower")
        guide = path / "doc/testing.md"
        first = DOC.replace("in parallel.\n", "in parallel.\nEach gate runs 4 processes.\n")
        guide.write_text(first)
        (path / "a.py").write_text("a\n")
        (path / "CHANGELOG.md").write_text(changelog("- Mine A"))
        self.git(path, "add", "doc/testing.md", "a.py", "CHANGELOG.md")
        self.git(path, "commit", "-q", "-m", "Add A")
        lower = self.git(path, "rev-parse", "HEAD")
        self.git(path, "switch", "-q", "-c", "upper")
        second = first.replace("4 processes", "4 xctest processes")
        guide.write_text(second)
        (path / "b.py").write_text("b\n")
        (path / "CHANGELOG.md").write_text(changelog("- Mine B", "- Mine A"))
        self.git(path, "add", "doc/testing.md", "b.py", "CHANGELOG.md")
        self.git(path, "commit", "-q", "-m", "Add B")
        upper = self.git(path, "rev-parse", "HEAD")

        self.ahead(doc=lambda text: text.replace("## Devices", "## Simulators"))
        on_main = lambda text: text.replace("## Devices", "## Simulators")
        one = self.land_as(lower, {"a.py": "a\n", "doc/testing.md": on_main(first),
                                   "CHANGELOG.md": changelog("- Mine A", "- Theirs")})
        two = self.land_as(upper, {"b.py": "b\n", "doc/testing.md": on_main(second),
                                   "CHANGELOG.md": changelog("- Mine B", "- Mine A", "- Theirs")})
        self.git(self.main, "pull", "-q", "--ff-only", "origin", "main")
        self.commit(self.main, "doc/testing.md", on_main(second).replace("4 xctest", "6 xctest"), "Count six")
        self.publish()
        for added in ("Each gate runs 4 processes.", "Each gate runs 4 xctest processes."):
            self.assertNotIn(added, self.git(self.main, "show", "origin/main:doc/testing.md"))

        where = "whose patch differs only in where its lines sit in CHANGELOG.md, doc/testing.md"
        self.assertEqual(self.proof("origin/main", lower),
                         (landing_proofs.RESOLVED, f"{lower[:12]} as {one[:12]}, {where}"))
        self.assertEqual(self.proof("origin/main", upper),
                         (landing_proofs.RESOLVED, f"{lower[:12]} as {one[:12]}, {where}; {upper[:12]} as {two[:12]}, {where}"))

    def test_a_stacked_commit_whose_guide_line_landed_changed_proves_nothing(self):
        path = self.worktree("lower")
        (path / "doc/testing.md").write_text(DOC.replace("in parallel.\n", "in parallel.\nEach gate runs 4 processes.\n"))
        (path / "CHANGELOG.md").write_text(changelog("- Mine A"))
        self.git(path, "add", "doc/testing.md", "CHANGELOG.md")
        self.git(path, "commit", "-q", "-m", "Add A")
        tip = self.git(path, "rev-parse", "HEAD")
        self.ahead(doc=lambda text: text.replace("## Devices", "## Simulators"))
        self.land_as(tip, {"doc/testing.md": DOC.replace("## Devices", "## Simulators").replace(
            "in parallel.\n", "in parallel.\nEach gate runs 4 process.\n"), "CHANGELOG.md": changelog("- Mine A", "- Theirs")})
        self.assertEqual(self.proof("origin/main", tip), (None, ""))


if __name__ == "__main__":
    unittest.main()
