"""Each platform plugin is named wherever the docs list the plugins, and in the README's diagram (vocaro/queuekeeper#253)."""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import config

ROOT = Path(__file__).resolve().parents[2]
# Each document that lists the plugins, the heading of the section holding its list, and how it reaches plugins/.
LISTS = [("README.md", "## Plugins", ""),
         ("doc/developer_guide.md", "### Plugins and extensions", "../"),
         ("doc/user_guide.md", "## Further reading", "../")]
DIAGRAM = "assets/overview.svg"


def platform_plugins(root):
    """Each platform plugin's README path from the root, from the workspace and from plugins/ alike.

    A visibility plugin is left out: the README and guides list those under Visibility features instead."""
    workspace = json.loads((root / "queuekeeper-workspace.json").read_text())
    readmes = {f"{entry['source']}/{package}/README.md": entry["source"].split("/")[1]
               for package, entry in workspace.items() if "visibility" not in entry}
    folders = {path.name for path in (root / "plugins").iterdir()
               if path.is_dir() and not path.name.startswith("visibility-")}
    return readmes, sorted(folders - set(readmes.values()))


def section(text, heading):
    """The text under a Markdown heading, up to the next heading of its level or above."""
    level = heading.split()[0]
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    end = re.search(rf"^#{{1,{len(level)}}} ", text[start:], re.M)
    return text[start:start + end.start()] if end else text[start:]


def omissions(root):
    """Each place a platform plugin is missing from, as a sentence."""
    readmes, unlisted = platform_plugins(root)
    failures = [f"plugins/{folder} is not in queuekeeper-workspace.json" for folder in unlisted]
    names = {}
    for name, heading, prefix in LISTS:
        listed = section((root / name).read_text(), heading)
        for readme in sorted(readmes):
            link = re.search(rf"\[([^\]]+)\]\({re.escape(prefix + readme)}\)", listed)
            if link is None:
                failures.append(f"{name}'s {heading.lstrip('# ')} does not link {readme}")
            elif name == "README.md":
                names[readme] = link.group(1)
    labels = set(re.findall(r"<text\b[^>]*>([^<]*)</text>", (root / DIAGRAM).read_text()))
    failures += [f"{DIAGRAM} has no box for the {label} plugin" for label in sorted(names.values()) if label not in labels]
    return failures


class PluginLists(unittest.TestCase):
    def test_each_platform_plugin_is_in_every_list_and_the_diagram(self):
        readmes, _ = platform_plugins(ROOT)
        self.assertIn("plugins/android/src/queuekeeper_android/README.md", readmes)
        self.assertNotIn("plugins/visibility-watch/src/queuekeeper_watch/README.md", readmes)
        self.assertEqual(omissions(ROOT), [])

    def test_a_plugin_missing_from_a_list_or_the_diagram_is_reported(self):
        readme = "plugins/new/src/queuekeeper_new/README.md"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "doc").mkdir()
            (root / "assets").mkdir()
            for folder in ("new", "stray", "visibility-extra"):
                (root / "plugins" / folder).mkdir(parents=True)
            (root / "queuekeeper-workspace.json").write_text(json.dumps({
                "queuekeeper_new": {"source": "plugins/new/src"},
                "queuekeeper_extra": {"source": "plugins/visibility-extra/src", "visibility": "extra"}}))
            (root / "README.md").write_text(f"# Q\n\n## Plugins\n\n- **[New]({readme})** does things.\n\n## Next\n")
            # The developer guide links it only after its section ends; the user guide names it with no link.
            (root / "doc/developer_guide.md").write_text(
                f"# D\n\n### Plugins and extensions\n\nNone.\n\n## Later\n\n[New](../{readme})\n")
            (root / "doc/user_guide.md").write_text("# U\n\n## Further reading\n\n- The New plugin.\n")
            (root / DIAGRAM).write_text('<svg><text x="1">Plugins</text><text x="2">Newer</text></svg>')
            self.assertEqual(omissions(root), [
                "plugins/stray is not in queuekeeper-workspace.json",
                f"doc/developer_guide.md's Plugins and extensions does not link {readme}",
                f"doc/user_guide.md's Further reading does not link {readme}",
                f"{DIAGRAM} has no box for the New plugin"])
            (root / "doc/developer_guide.md").write_text(f"# D\n\n### Plugins and extensions\n\n[New](../{readme})\n")
            (root / "doc/user_guide.md").write_text(f"# U\n\n## Further reading\n\n- [New](../{readme})\n")
            (root / DIAGRAM).write_text('<svg><text x="2">New</text></svg>')
            (root / "plugins/stray").rmdir()
            self.assertEqual(omissions(root), [])

    def test_a_docs_only_change_to_a_list_runs_this_module(self):
        # A docs-only landing skips the gate, which runs this module; a change to a document it reads must run it
        # anyway, or the next code entry's gate fails on a document it did not touch (vocaro/queuekeeper#147).
        project = config.load(ROOT)
        for name, _, _ in LISTS:
            with self.subTest(name=name):
                named = config.unread_named(project, [name])
                self.assertTrue(any(__name__.rpartition(".")[2] in " ".join(project["lanes"][lane]["command"])
                                    for lane in named), f"no lane queue.unread names for {name} runs {__name__}")


if __name__ == "__main__":
    unittest.main()
