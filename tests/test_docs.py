"""The Markdown link and anchor check."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import docs


class DocumentationTests(unittest.TestCase):
    def test_case_mismatches_missing_links_and_large_instructions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target.md").write_text("# Target\n")
            (root / "README.md").write_text("[fine](target.md)\n[wrong](Target.md)\n[missing](absent.md)\n")
            (root / "AGENTS.md").write_text("x" * 32768)
            failures = docs.check(root)
            self.assertEqual(len(failures), 3, failures)

    def test_default_docs_folder_and_markdown_extension_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/topic.markdown").write_text("[missing](absent.md)\n")
            failures = docs.check(root)
            self.assertEqual(len(failures), 1, failures)
            self.assertIn("missing link: absent.md", failures[0])

    def test_a_fragment_must_name_a_heading_or_anchor_of_its_markdown_target(self):
        target = ("# Target\n\n## Repeated\n\n## Repeated\n\n## The `eng docs` check — 0.3.1\n\n"
                  "## [Linked](elsewhere.md) __init__ and C#\n\n"
                  '<a id="stacks"></a>\n\nSetext title\n------------\n\n- a list item\n---\n\n'
                  "```sh\n# not a heading\n```\n")
        links = ("[a](target.md#repeated) [b](target.md#repeated-1) [c](target.md#the-eng-docs-check--031)\n"
                 "[d](target.md#linked-init-and-c) [e](target.md#stacks) [f](target.md#setext-title)\n"
                 "[g](#local) [h](notes.txt#L3) [i](target.md)\n\n## Local\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "target.md").write_text(target)
            (root / "notes.txt").write_text("a\nb\nc\n")
            (root / "README.md").write_text(links)
            self.assertEqual(docs.check(root, ["README.md"]), [])
            # Renaming a heading strands the links to its old anchor.
            (root / "target.md").write_text(target.replace("Setext title", "Setext heading"))
            (root / "README.md").write_text(links + "[j](target.md#repeated-2) [k](target.md#not-a-heading)\n"
                                            "[l](target.md#a-list-item) [m](#nowhere)\n")
            self.assertEqual(docs.check(root, ["README.md"]), [
                f"{root / 'README.md'}: missing anchor: target.md#setext-title",
                f"{root / 'README.md'}: missing anchor: target.md#repeated-2",
                f"{root / 'README.md'}: missing anchor: target.md#not-a-heading",
                f"{root / 'README.md'}: missing anchor: target.md#a-list-item",
                f"{root / 'README.md'}: missing anchor: #nowhere"])

    def test_a_path_line_link_must_name_a_line_of_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            readme = root / "doc/README.md"
            (root / "src").mkdir()
            (root / "doc").mkdir()
            (root / "src/Foo.cs").write_text("a\nb\nc")        # three lines, the last unterminated
            (root / "doc/named:7").write_text("one line\n")    # a file whose whole name ends in :7
            readme.write_text("[a](../src/Foo.cs:1) [b](../src/Foo.cs:3) [c](./named:7)\n")
            self.assertEqual(docs.check(root, ["doc/README.md"]), [])
            # A line past the end, no line 0, a missing file, a range, a directory and a wrong case.
            readme.write_text("[d](../src/Foo.cs:4) [e](../src/Foo.cs:0) [f](../src/Absent.cs:1)\n"
                              "[g](../src/Foo.cs:1-2) [h](../src:1) [i](../src/foo.cs:2)\n")
            self.assertEqual(docs.check(root, ["doc/README.md"]), [
                f"{readme}: no line 4 in a 3-line file: ../src/Foo.cs:4",
                f"{readme}: no line 0 in a 3-line file: ../src/Foo.cs:0",
                f"{readme}: missing link: ../src/Absent.cs:1",
                f"{readme}: missing link: ../src/Foo.cs:1-2",
                f"{readme}: line link to a directory: ../src:1",
                f"{readme}: wrong-case link: ../src/foo.cs:2"])

    def test_heading_slugs_follow_github(self):
        for heading, expected in [("Concurrent Git integration", "concurrent-git-integration"),
                                  ("0.3.1 release verification", "031-release-verification"),
                                  ("A — B", "a--b"), ("foo_bar & `x.y`", "foo_bar--xy"),
                                  ("Ünïcode café", "ünïcode-café"), ("**Bold** *it*", "bold-it")]:
            with self.subTest(heading):
                self.assertEqual(docs.slug(docs.heading_text(heading)), expected)


if __name__ == "__main__":
    unittest.main()
