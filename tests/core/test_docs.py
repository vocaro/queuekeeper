"""The Markdown link and anchor check, and the check of citations by path and section title."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, config, docs


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
            missing = f"{root / 'README.md'}: missing anchor: "
            self.assertEqual(docs.check(root, ["README.md"]), [
                missing + "target" + ".md#setext-title", missing + "target" + ".md#repeated-2",
                missing + "target" + ".md#not-a-heading", missing + "target" + ".md#a-list-item",
                missing + "#nowhere"])

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


# The citations below name documents that exist only in each test's temporary checkout, so this file spells
# their suffix through MD, which the citation check of this checkout does not read.
MD = ".md"


class CitationTests(unittest.TestCase):
    """Citations of a guide by path and section title, or by path and anchor (vocaro/queuekeeper#114)."""
    GUIDE = ("# Host\n\n## Heavy-work admission\n\nText.\n\n**Landing ahead of the queue.** Text.\n\n"
             "- **A `qk` lead-in**: text\n\n## The second Mac\n\n```sh\n# Not a heading\n```\n")

    def checkout(self, directory, code):
        root = Path(directory).resolve()
        (root / "guides").mkdir()
        (root / "src").mkdir()
        (root / f"guides/host{MD}").write_text(self.GUIDE)
        (root / f"guides/lanes{MD}").write_text("# Lanes\n\nSee host" + MD + ", The second Mac.\n")
        (root / "src/tool.py").write_text(code)
        # The whole check reads the files git tracks.
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        return root

    def test_a_citation_must_name_a_heading_or_bold_lead_in_of_an_existing_document(self):
        code = (f'"""Admits heavy work (guides/host{MD}, Heavy-work admission; guides/host{MD}, Landing\n'
                f'ahead of the queue)."""\n'
                f"# Wraps in a comment (guides/host{MD},\n# The second Mac), and in strings:\n"
                f'HINT = ("run it alone (guides/host{MD}, The "\n        f"second Mac); as guides/host{MD}#the-second-mac says")\n'
                f'LEAD = "guides/host{MD}, A qk lead-in"\n'
                f"# Lists and prose: guides/host{MD}, guides/lanes{MD}, and the rest; guides/host{MD}, which says so.\n"
                f"# Another repository's: the toolkit's guides/releasing{MD}, Keeping the split.\n"
                f"# A Markdown link's target is the link check's: [x](absent{MD}#nowhere)\n")
        with tempfile.TemporaryDirectory() as directory:
            root = self.checkout(directory, code)
            self.assertEqual(docs.check(root, citations=True), [])
            (root / "src/tool.py").write_text(
                code + f"# (guides/host{MD}, Setting up a build host), (guides/host{MD}, Heavy work)\n"
                f"# (guides/gone{MD}, Heavy-work admission), guides/host{MD}#heavy-work, (guides/host{MD}, Not a heading)\n")
            (root / f"guides/lanes{MD}").write_text("# Lanes\n\nSee host" + MD + ", The first Mac.\n")
            tool, lanes = root / "src/tool.py", root / f"guides/lanes{MD}"
            self.assertEqual(docs.check(root), [])   # off unless the project turns it on
            self.assertEqual(docs.check(root, citations=True), [
                f"{lanes}:3: cites no heading or bold lead-in of host{MD}: The first Mac.",
                f"{tool}:11: cites no heading or bold lead-in of guides/host{MD}: Setting up a build host",
                f"{tool}:11: cites no heading or bold lead-in of guides/host{MD}: Heavy work",
                f"{tool}:12: cites a missing document: guides/gone{MD}",
                f"{tool}:12: cites a missing anchor: guides/host{MD}#heavy-work",
                f"{tool}:12: cites no heading or bold lead-in of guides/host{MD}: Not a heading"])

    def test_named_files_are_the_only_ones_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.checkout(directory, f"# (guides/host{MD}, Heavy work)\n")
            self.assertEqual(docs.check(root, [f"guides/lanes{MD}"], citations=True), [])
            self.assertEqual(len(docs.check(root, ["src/tool.py"], citations=True)), 1)
            self.assertEqual(docs.check(root, ["src/tool.py"]), [])

    def test_data_logs_records_and_code_after_a_comma_are_not_read(self):
        stale = f"(guides/host{MD}, Heavy work)\n"
        with tempfile.TemporaryDirectory() as directory:
            root = self.checkout(directory, f'GUIDES = {{"guides/host{MD}": 1}}; pick(guides/host{MD}, Path(x), [y])\n'
                                            f"# guides/host{MD}, {{'a': 1}}\n")
            for name in ["data/manifest.json", "lane.log", "notes.txt", "evidence/run/README.md",
                         "doc/archive/old.md", "vendor/Vendor/lib.py"]:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(stale)
            (root / "schemas").mkdir()
            (root / "schemas/project.schema.json").write_text('{"description": "' + stale.strip() + '"}\n')
            (root / "scripts").mkdir()
            (root / "scripts/run").write_text("#!/bin/sh\n# " + stale)
            (root / "scripts/plain").write_text(stale)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            self.assertEqual(docs.check(root, citations=True), [
                f"{root / 'schemas/project.schema.json'}:1: cites no heading or bold lead-in of guides/host{MD}: Heavy work",
                f"{root / 'scripts/run'}:2: cites no heading or bold lead-in of guides/host{MD}: Heavy work"])


class CitationSettingTests(unittest.TestCase):
    """queuekeeper.json's docs.citations turns the citation check on for a project (vocaro/queuekeeper#114)."""
    def test_docs_takes_only_a_boolean_citations_and_the_schema_lists_it(self):
        self.assertTrue(config.parse(json.dumps({"schemaVersion": 1, "name": "Example",
                                                 "docs": {"citations": True}}))["docs"]["citations"])
        for docs_value, message in [({"citations": "yes"}, "docs.citations must be true or false"),
                                    ({"links": True}, "docs supports only citations"),
                                    (True, "docs supports only citations")]:
            with self.subTest(docs=docs_value), self.assertRaisesRegex(ValueError, message):
                config.parse(json.dumps({"schemaVersion": 1, "name": "Example", "docs": docs_value}))
        schema = json.loads((config.TOOLKIT_ROOT / "schemas/queuekeeper.schema.json").read_text())
        self.assertEqual(schema["properties"]["docs"]["properties"]["citations"]["type"], "boolean")
        self.assertTrue(config.load(config.TOOLKIT_ROOT)["docs"]["citations"])   # Queuekeeper checks its own

    def test_qk_docs_check_reads_citations_only_when_the_project_turns_them_on(self):
        with tempfile.TemporaryDirectory() as directory:
            root = CitationTests().checkout(directory, f"# (guides/host{MD}, Heavy work)\n")
            for setting, status in [({}, 0), ({"docs": {"citations": False}}, 0), ({"docs": {"citations": True}}, 1)]:
                with self.subTest(setting=setting):
                    (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example",
                                                                       "extensions": [], **setting}))
                    with contextlib.redirect_stdout(io.StringIO()) as printed:
                        self.assertEqual(cli.main(["--project", str(root), "docs", "check"]), status,
                                         printed.getvalue())


class CommandTests(unittest.TestCase):
    """Each `qk WORD` the docs tell a reader to type names one of qk's subcommands (vocaro/queuekeeper#39)."""
    TEXT = ("# Setup\n\nRun `qk visibility enable dashboard`, then `python3 scripts/qk --project . sevre --open`.\n"
            "`qk COMMAND` and `qk --help` are no command's words, nor is the `qk` in prose.\n\n"
            "```sh\nqk land --wait\n~/.local/bin/qk lnad\ncd x && python3 scripts/qk queue status  # qk queue\n```\n\n"
            "```python\nrun(['qk', 'nothing'])  # qk nothing, in code that is not a shell's\n```\n\n"
            "```\n$ qk serve --open\n```\n\n~~~zsh\nqk unittset -s tests\n~~~\n")

    def test_each_typed_command_is_read_from_shell_blocks_and_code_spans(self):
        self.assertEqual(docs.commands_typed(self.TEXT), [
            (3, "visibility"), (3, "sevre"), (7, "land"), (8, "lnad"), (9, "queue"), (9, "queue"), (17, "serve"),
            (21, "unittset")])

    def test_a_word_that_is_no_subcommand_is_reported_with_its_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "README.md").write_text(self.TEXT)
            known = {"visibility", "land", "queue", "serve", "unittest"}
            readme = root / "README.md"
            self.assertEqual(docs.check(root, commands=known), [
                f"{readme}:3: names no qk command: qk sevre", f"{readme}:8: names no qk command: qk lnad",
                f"{readme}:21: names no qk command: qk unittset"])
            self.assertEqual(docs.check(root), [])   # with no subcommand names given, none is read

    def test_qk_docs_check_knows_every_subcommand_features_and_plugins_included(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "queuekeeper.json").write_text(json.dumps({"schemaVersion": 1, "name": "Example", "extensions": []}))
            # serve and watch are visibility features' commands, unity a plugin's, none of them loaded here.
            (root / "README.md").write_text("`qk land`, `qk docs check`, `qk serve --open`, `qk watch`, `qk unity test`\n")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["--project", str(root), "docs", "check"]), 0, printed.getvalue())
            (root / "README.md").write_text("Then type `qk dashboard`.\n")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["--project", str(root), "docs", "check"]), 1)
            self.assertEqual(printed.getvalue().strip(), f"{root / 'README.md'}:1: names no qk command: qk dashboard")


if __name__ == "__main__":
    unittest.main()
