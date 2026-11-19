"""Portable Markdown link and anchor checks."""
import html
from pathlib import Path
import re
import os
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit

MARKDOWN = {".md", ".markdown"}
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
ATX = re.compile(r" {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
BREAK = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
# Lines that open a block other than a paragraph, which no setext underline turns into a heading.
OTHER_BLOCK = re.compile(r" {4}|\t| {0,3}(?:[-*+][ \t]|\d{1,9}[.)][ \t]|[>|<])")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
HTML_ANCHOR = re.compile(r"<[A-Za-z][^>]*?\s(?:id|name)\s*=\s*([\"'])(.*?)\1")
# A link to a source line, PATH:LINE, as in [kernel](../Shaders/Grid.hlsl:180).
LINE_LINK = re.compile(r"(.+):(\d+)")
# A paragraph's bold lead-in, which a citation may name as it names a heading.
BOLD_LEAD = re.compile(r" {0,3}(?:[-*+][ \t]+|\d{1,9}[.)][ \t]+)?\*\*(.+?)\*\*")
VENDORED = {"licenses", "ThirdParty", "Vendor"}
# A citation of a document by its path, from the checkout's root or the citing file's folder, and a
# section title or an anchor: doc/setup.md, Install or doc/setup.md#install.
CITED = re.compile(r"(?<![\w./\\-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:md|markdown))(?:(#[\w-]+)|,[ \t]*)")
# Another repository's document, as in the toolkit's doc/setup.md: the words before its path.
OTHER_REPOSITORY = re.compile(r"[\w-]+'s\s*$")
# What ends a line of a comment or a string literal that a citation wraps across, and what opens the next.
WRAP_END = re.compile(r"""[\s"'+\\]*$""")
WRAP_START = re.compile(r"""^\s*(?:(?:#+|//+|/?\*+|--|>|\+)\s*)*(?:[rbfuRBFU]{0,2}(?:\"\"\"|'''|"|'))?""")
# A text file larger than this is data, not prose that cites a guide.
LARGEST_CITING = 2 << 20
# The files whose citations are read: Markdown, source code, scripts (an extensionless file that starts #!) and
# JSON schemas, whose descriptions cite the guides. Data, logs and plain text are not read, nor recorded evidence
# and archives, which quote what a guide said when they were written.
SOURCE = MARKDOWN | {".py", ".swift", ".m", ".mm", ".h", ".hpp", ".c", ".cc", ".cpp", ".cs", ".js", ".mjs", ".ts",
                     ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".rb", ".go", ".rs", ".java", ".kt", ".kts", ".gradle",
                     ".toml", ".yml", ".yaml", ".hlsl", ".shader", ".metal"}
RECORDS = {"evidence", "archive", "archives", "archived"}
# What no section title holds, which marks text after a path's comma as code or data: braces, brackets,
# operators, or a call such as Path(x).
NOT_A_TITLE = re.compile(r"[{}\[\]<>=$|\\]|\w\(")
# The info strings of a fenced block of shell commands; a block with none is read as one too.
SHELL = {"", "sh", "bash", "zsh", "shell", "console", "shell-session", "sh-session"}
# A command line's `qk WORD`, by any path to the command, as in python3 scripts/qk land or ~/.local/bin/qk doctor,
# with --project before the word if given. A word in capitals, such as qk COMMAND, is a placeholder.
QK_COMMAND = re.compile(r"(?<![\w.$-])(?:\S*/)?qk(?:[ \t]+--project(?:=\S+|[ \t]+\S+))?[ \t]+([a-z][a-z0-9-]*)(?![\w-])")


def slug(text):
    """GitHub's heading anchor: lowercase, keeping only word characters, hyphens and spaces, and
    each space a hyphen. Word characters are letters, marks, decimal digits and connectors (_)."""
    return "".join("-" if c == " " else c for c in text.lower()
                   if c in " -" or unicodedata.category(c)[0] in "LM" or unicodedata.category(c) in ("Nd", "Pc"))


def heading_text(source):
    """A heading's text as GitHub renders it: code spans verbatim, link and emphasis markup gone."""
    def inline(text):
        text = re.sub(r"!?\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])", r"\1", text)
        text = re.sub(r"<[^>]*>", "", text)
        text = re.sub(r"(?<![\w\\])(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
        return html.unescape(re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text))
    pieces, last = [], 0
    for span in CODE_SPAN.finditer(source):
        code = span[2]
        if code.startswith(" ") and code.endswith(" ") and code.strip(): code = code[1:-1]
        pieces += [inline(source[last:span.start()]), code]
        last = span.end()
    return "".join(pieces + [inline(source[last:])]).strip()


def sections(text):
    """A Markdown document's headings, each as GitHub renders its text, in order, and the bold lead-ins
    that open its paragraphs and list items, such as **Landing ahead of the queue.**, without the closing
    stop: what a citation by path and section title may name. Also the HTML ids it defines."""
    headings, leads, ids = [], [], set()
    fence, paragraph = None, []
    for line in text.splitlines():
        if fence:
            if re.match(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", line): fence = None
            continue
        opening = FENCE.match(line)
        if opening:
            fence, paragraph = opening[1], []
            continue
        ids.update(match[2] for match in HTML_ANCHOR.finditer(CODE_SPAN.sub("", line)))
        lead = BOLD_LEAD.match(line)
        if lead and (not paragraph or OTHER_BLOCK.match(line)):
            leads.append(heading_text(lead[1]).rstrip(".:"))
        atx = ATX.match(line)
        if not line.strip(): paragraph = []
        elif paragraph and SETEXT.match(line): headings.append(heading_text("\n".join(paragraph))); paragraph = []
        elif atx: headings.append(heading_text(atx[1] or "")); paragraph = []
        elif BREAK.match(line): paragraph = []
        elif OTHER_BLOCK.match(line): paragraph = None   # until the next blank line
        elif paragraph is not None: paragraph.append(line.strip())
    return headings, leads, ids


def anchors(text):
    """The fragments GitHub defines for a Markdown document: heading slugs and HTML ids."""
    headings, _, ids = sections(text)
    return anchors_from(headings, ids)


def anchors_from(headings, ids):
    """The fragments of a document with these heading texts, in order, and these HTML ids."""
    found, occurrences = set(ids), {}
    for heading in headings:
        base = slug(heading)
        if not base: continue
        name = base
        while name in occurrences:  # a repeated heading gets -1, -2, as GitHub numbers them
            occurrences[base] += 1
            name = f"{base}-{occurrences[base]}"
        occurrences[name] = 0
        found.add(name)
    return found


def commands_typed(text):
    """Each (line number, word) of a `qk WORD` that text, a Markdown document, tells a reader to type: in a fenced
    block of shell commands, or in a code span outside any fenced block."""
    found, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if fence:
            if re.match(rf" {{0,3}}{re.escape(fence[0][0])}{{{len(fence[0])},}}[ \t]*$", line): fence = None
            elif fence[1]: found += [(number, match[1]) for match in QK_COMMAND.finditer(line)]
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})[ \t]*([^\s`{]*)", line)
        if opening:
            fence = (opening[1], opening[2].lower() in SHELL)
            continue
        found += [(number, match[1]) for span in CODE_SPAN.finditer(line) for match in QK_COMMAND.finditer(span[2])]
    return found


def check(root, paths=None, citations=False, commands=()):
    """Each failure of the project's Markdown: a missing link, anchor or source line, a wrong-case link, and an
    AGENTS.md of 32 KiB or more. With citations, queuekeeper.json's docs.citations, also each citation that names
    no document or section of this checkout (cited). With commands, the names of qk's subcommands, also each
    `qk WORD` a reader is told to type whose word is none of them (vocaro/queuekeeper#39)."""
    root = Path(root).resolve()
    failures = []
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=30)
    tracked_names = {os.fsdecode(p) for p in tracked.stdout.split(b"\0") if p}
    tracked_lower = {p.lower() for p in tracked_names}
    defined = {}
    def missing_anchor(document, fragment):
        if document not in defined: defined[document] = anchors(document.read_text())
        return fragment not in defined[document]
    if paths is not None:
        files = paths
    else:
        # Current subject docs are maintained; historical measurement attachments may be
        # intentionally external/ignored and must not be rewritten by a general doc check.
        files = [p for suffix in MARKDOWN for p in root.glob(f"*{suffix}")]
        for folder in ["doc", "docs", "guides", "knowledge", "agents", "policies"]:
            files.extend(p for suffix in MARKDOWN for p in (root / folder).rglob(f"*{suffix}")
                if not any(part.startswith(".") or part in VENDORED
                           for part in p.relative_to(root / folder).parts))
    for path in files:
        path = Path(path)
        if not path.is_absolute(): path = root / path
        if path.name == "AGENTS.md" and path.stat().st_size >= 32768:
            failures.append(f"{path}: agent instructions reach 32 KiB")
        whole = path.read_text()
        if commands:
            failures += [f"{path}:{number}: names no qk command: qk {word}"
                         for number, word in commands_typed(whole) if word not in commands]
        text = re.sub(r"```.*?```", "", whole, flags=re.S)
        for raw in re.findall(r"\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
            target = unquote(raw.strip("<>"))
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            if target.startswith("#"):
                if url.fragment and missing_anchor(path.resolve(), url.fragment):
                    failures.append(f"{path}: missing anchor: {raw}")
                continue
            relative = url.path
            if not relative: continue
            # PATH:LINE names a line of PATH, unless a file's whole name is the target.
            line = LINE_LINK.fullmatch(relative)
            if line and not (path.parent / relative).exists(): relative = line[1]
            else: line = None
            destination = path.parent / relative
            if not destination.resolve().is_relative_to(root):
                failures.append(f"{path}: link escapes checkout: {raw}")
                continue
            if not destination.exists():
                failures.append(f"{path}: missing link: {raw}")
                continue
            # Only a Markdown target's fragments are known here; a source line's (#L10) is not.
            if (url.fragment and not url.query and destination.suffix.lower() in MARKDOWN
                    and destination.is_file() and missing_anchor(destination.resolve(), url.fragment)):
                failures.append(f"{path}: missing anchor: {raw}")
            if line and not destination.is_file():
                failures.append(f"{path}: line link to a directory: {raw}")
            elif line:
                count = len(destination.read_bytes().splitlines())
                if not 1 <= int(line[2]) <= count:
                    failures.append(f"{path}: no line {line[2]} in a {count}-line file: {raw}")
            # Git names are canonical even if a case-insensitive worktree directory was
            # created with another spelling by a build tool. Untracked files use disk names.
            normalized = str(Path(os.path.normpath(destination)).relative_to(root))
            if normalized in tracked_names or any(p.startswith(normalized.rstrip("/") + "/") for p in tracked_names):
                continue
            if normalized.lower() in tracked_lower:
                failures.append(f"{path}: wrong-case link: {raw}")
                continue
            # macOS may resolve the wrong case; verify untracked spellings component by component.
            probe = path.parent
            for part in Path(relative).parts:
                if part == "..": probe = probe.parent
                elif part != ".":
                    if part not in {p.name for p in probe.iterdir()}:
                        failures.append(f"{path}: wrong-case link: {raw}")
                        break
                    probe = probe / part
    if not citations: return failures
    reading = files if paths is not None else [root / name for name in sorted(tracked_names)]
    return failures + cited(root, reading)


def citing(root, path):
    """Whether cited reads path, a file of the checkout at root: Markdown, source, a script or a JSON schema,
    outside dot, vendored and record folders."""
    try: parts = path.relative_to(root).parts
    except ValueError: parts = path.parts
    if any(part.startswith(".") or part in VENDORED or part.lower() in RECORDS for part in parts[:-1]): return False
    suffix = path.suffix.lower()
    if suffix in SOURCE or path.name.endswith(".schema.json"): return True
    if suffix: return False
    try:
        with open(path, "rb") as file: return file.read(2) == b"#!"
    except OSError:
        return False


def cited(root, files):
    """Each citation in files of a Markdown document by its path and a section title, as in
    (doc/setup.md, Install), or by its path and an anchor, that names no document
    in this checkout, or no heading or bold lead-in of it (vocaro/queuekeeper#114). The path is from
    the checkout's root or the citing file's folder. A title may wrap across lines of a comment or of
    adjacent string literals, and it may run on past the section's name. A citation of another
    repository's document, as in the toolkit's doc/setup.md, is not read, since this checkout
    cannot resolve it, and a Markdown link's target is the link check's."""
    root = Path(root).resolve()
    failures, read = [], {}
    def document(cited, citing):
        for base in (root, citing.parent):
            target = (base / cited).resolve()
            if target.is_relative_to(root) and target.is_file():
                if target not in read: read[target] = sections(target.read_text(errors="replace"))
                return target
        return None
    for path in files:
        path = Path(path)
        if not path.is_absolute(): path = root / path
        try:
            if not citing(root, path): continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > LARGEST_CITING: continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw or (b".md" not in raw and b".markdown" not in raw): continue
        try: text = raw.decode()
        except UnicodeDecodeError: continue
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            for match in CITED.finditer(line):
                before = line[:match.start()]
                if before.endswith("]("): continue
                if not WRAP_START.sub("", before).strip() and number > 1:
                    before = WRAP_END.sub("", lines[number - 2])
                if OTHER_REPOSITORY.search(before): continue
                cited, anchor = match[1], match[2]
                rest = line[match.end():]
                tail = " ".join([WRAP_END.sub("", rest)] + [WRAP_END.sub("", WRAP_START.sub("", following))
                                                           for following in lines[number:number + 2]])
                tail = " ".join(tail.replace("`", "").split())
                # A title begins with a capital or a code span; anything else is a comma in prose or a list.
                if not anchor and (not tail or not (tail[0].isupper() or rest.lstrip().startswith("`"))
                                   or re.match(r"\S+\.(?:md|markdown)\b", tail)):
                    continue
                # Braces, brackets or operators before the title closes are code or data, not a title.
                if not anchor and NOT_A_TITLE.search(re.split(r"""[);"]""", rest if rest.strip() else tail)[0]):
                    continue
                target = document(cited, path)
                where = f"{path}:{number}"
                if target is None:
                    failures.append(f"{where}: cites a missing document: {cited}")
                    continue
                headings, leads, ids = read[target]
                if anchor:
                    if anchor[1:] not in anchors_from(headings, ids):
                        failures.append(f"{where}: cites a missing anchor: {cited}{anchor}")
                    continue
                titles = {" ".join(title.replace("`", "").split()) for title in headings + leads} - {""}
                if not any(tail.startswith(title) and not tail[len(title):len(title) + 1].isalnum()
                           for title in titles):
                    shown = re.split(r"""[);"']""", tail)[0]   # where a title in parentheses or a string ends
                    shown = shown if len(shown) <= 60 else shown[:57] + "..."
                    failures.append(f"{where}: cites no heading or bold lead-in of {cited}: {shown}")
    return failures
