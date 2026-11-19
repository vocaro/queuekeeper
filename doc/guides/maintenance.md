# Maintenance

Rules for changing Queuekeeper itself and keeping its repository in order: editing it while work
runs from its checkout, the copies each project keeps, setting up the signed queue launcher,
retaking the README's screenshots, and the spelling and docs checks. Read it before changing
Queuekeeper or its documentation. The merge queue's rules moved to [the merge queue](merge-queue.md#the-merge-queue).

## Changing Queuekeeper while work runs

**Edit only in a linked worktree.** Do not edit, commit in or move the main checkout while a lane is
dispatched from it: a dispatch sends a snapshot of that checkout's tree, and a run that finds the
tree changed since the send does not run, saying "restore the tree that was sent, or run it again to
send the tree as it is now". A project's gate was lost this way on 2026-11-12. Land changes through
Queuekeeper's own queue, which moves the main checkout only when nothing runs from it.

**Keep the main checkout on `main`.** Every project's launcher loads Queuekeeper from it, so a branch
there runs that branch's code for every `qk`. While the loaded Queuekeeper is a main checkout on
another branch, or detached at a commit neither `main` nor `origin/main` holds, every command says so
first on standard error, `queuekeeper: Queuekeeper loads from PATH, on branch NAME at COMMIT, not
main, ...`, and `qk doctor` lists it among its warnings. A linked worktree says nothing. A checkout
once sat on a work branch for 45 minutes unnoticed ([#184](https://github.com/vocaro/queuekeeper/issues/184)).

## Copies in each project

Two files in every project are copies: `scripts/qk`, from this repository, and, in the owner's
projects, the toolkit's `scripts/_engineering.py`. A fix to either reaches a project only by a
landing there, a full gate each. Collect changes to either file into one, and expect one landing per
project ([#20](https://github.com/vocaro/queuekeeper/issues/20)).

## Signed queue launcher

Set this up once on the Mac that owns the queues. From the Queuekeeper checkout, build the app in its
build directory, then sign it in your Terminal with an Apple-issued signing identity.

```sh
app="$PWD/.build/Queuekeeper.app"
mkdir -p "$app/Contents/MacOS"
cp src/python/queuekeeper/_native/queue-launcher/Info.plist "$app/Contents/Info.plist"
python3 scripts/qk admit --here -- /usr/bin/clang -std=c11 -O2 -Wall -Wextra -Werror \
  src/python/queuekeeper/_native/queue-launcher/main.c -o "$app/Contents/MacOS/QueueLauncher"
/usr/bin/codesign --force --sign "Developer ID Application" --timestamp=none "$app"
/usr/bin/codesign --verify --strict "$app"
```

Install the app at a stable path and register it with Launch Services. When macOS prompts for
Local Network access, allow Queuekeeper. If the app is blocked from reaching another
Mac, check its access in System Settings under Privacy & Security, then Local Network.

```sh
mkdir -p "$HOME/Applications"
/usr/bin/ditto "$app" "$HOME/Applications/Queuekeeper.app"
app="$HOME/Applications/Queuekeeper.app"
/usr/bin/open -g -a "$app" --args /usr/bin/true
```

For each queue, run the following from its main checkout using the Python installation and
`PATH` its worker should use after login. Use the account's normal Git and SSH configuration for credentials.

```sh
draft="$(mktemp)"
python3 scripts/qk queue launcher-plist "$app" > "$draft" &&
  label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$draft")" &&
  mkdir -p "$HOME/Library/LaunchAgents" &&
  install -m 644 "$draft" "$HOME/Library/LaunchAgents/$label.plist" &&
  /bin/launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
rm -f "$draft"
```

`qk land`, `qk queue wait` and `qk queue resume` ask launchd to start the installed agent. At login
and on its interval the agent runs the same worker entry point. Empty and held queues return before
recovering dispatches or running gates, and the queue lock excludes duplicate workers. If launchd
rejects a start request, `worker.log` records the error and Queuekeeper starts the session worker.

### Removing the launcher

Close the queue, wait for it to be idle, boot the agent out and remove its plist:

```sh
python3 scripts/qk queue close --why "removing the queue's LaunchAgent" && {
  until python3 scripts/qk queue status --idle; do sleep 30; done &&
    /bin/launchctl bootout "gui/$(id -u)/$label" &&
    rm "$HOME/Library/LaunchAgents/$label.plist"
  python3 scripts/qk queue reopen
}
```

- **Wait on `--idle`.** The bootout ends a running worker and its gate. `qk queue status --idle`
  exits 0 only when no worker runs and no entry is queued, testing or passed, and 75 otherwise;
  plain `qk queue status` exits 0 mid-gate too, and a bootout chained on it once ended another
  session's gate ([#21](https://github.com/vocaro/queuekeeper/issues/21)). A worker between rounds
  with nothing waiting is still running, so `--idle` does not call it idle.
- **Close first.** An entry queued after an idle reading would start a worker the bootout then ends,
  so `qk queue close` comes first ([#32](https://github.com/vocaro/queuekeeper/issues/32)). Run
  `qk queue reopen` after, whether or not the removal went through, but only when this `close` was
  the one that closed the queue.
- A held queue with entries waiting never becomes idle: resume it, or remove its entries, before
  waiting.

## The README's screenshots

The README's pictures of the dashboard are taken from the dashboard itself, drawn from made-up data
that lives in the script. Retake them after a change to how the dashboard looks:

```sh
python3 tools/dashboard-screenshots.py
```

It needs Google Chrome and writes, with compact sample data in the dark theme:

- `assets/dashboard-home.svg`, the Queue home view, with its Flow map laid over the picture as the
  page drew it and the stylesheet's Flow rules written in, so the map's dots move by SVG animation in
  the README and stand still where the viewer asks for reduced motion;
- `dashboard-farm.png`.

**Run it directly, not through `qk admit`.** It starts one headless Chrome for a few seconds, needs
this Mac's Chrome and writes into this checkout. `qk admit` may send it to another Mac, which may
have no Chrome and would keep the pictures in its own checkout, so there the script refuses with
exit 2 ([#172](https://github.com/vocaro/queuekeeper/issues/172)). Its test still runs through
admission, as every test does: `tests/visibility/test_dashboard_screenshots.py` fails when a panel's
data no longer has the fields the script makes up, which is when to change the script.

## Spelling check

This is Queuekeeper's legacy spelling lane, which a later release removes: a project lists a
spelling check of its own as a preflight lane instead ([preflight lanes](../user_guide.md#preflight-lanes),
[#69](https://github.com/vocaro/queuekeeper/issues/69)).

Queuekeeper's own repository already does. Its `spelling` lane, the one entry of its
`queue.preflight`, runs `python3 scripts/check_spelling.py [PATHS]`, a standalone copy of this check
that imports nothing from the package, so it outlives the built-in one. Its exemptions are that
script's `PROJECT`, in place of queuekeeper.json's `spelling`, and it reads the subcommand names from
`python3 scripts/qk -h`. Until the built-in check is removed, a change to its rules is made in both.
The rest of this section describes both.

### What it finds

`python3 scripts/qk spelling [PATHS]` finds British spellings anywhere in a project's text,
identifiers included, and in its file and directory names, and exits 1 on any. Gate pushes on it:
the words arrive one at a time and are invisible in review.

- **What it reads.** With no paths, every tracked file, which is what a push carries; named paths
  also read untracked files Git does not ignore, so a page is checked before it is published from
  disk.
- **Words.** It splits each run of letters at camelCase, snake_case and SCREAMING_CASE boundaries and
  judges each subword whole, by pattern (-ise, -yse, -our, -tre, -bre and doubled-l inflections) and
  by an irregular list, with a list of American words that only look British. A pattern counts only
  the letters British words put before its ending, so a name, a foreign word or text that lost its
  spaces passes when its fix would not be a word: Louise, Apryse, bonjour, notre, septiembre,
  milesperhour. A string escape such as `\n` or JSON's `\u201c` ends a word.
- **File names.** Every file name is judged the same way, binary files included, split also at `/`,
  `.`, `-` and `_`, because references to a file must move when it is renamed. A hit reads
  `PATH: British spelling in file name 'WORD' -> AMERICAN`, and a directory is judged once, as
  `DIR/`.
- **Controls.** Its positive controls, for words and for file names, run before any file is read, so
  a detector that stopped detecting fails instead of passing. Exit 2 means nothing was checked.

The rules and what each exemption was learned from are in `src/python/queuekeeper/spelling.py`.

### Exemptions

It already exempts other projects' British-spelled API names, such as Apple's `isCanceled` and
Unity's `MonoBehaviour`, URLs, a list item's first `<em>` (a cited title) and a line marked
`spelling: keep` with a reason, such as a decoder reading a name stored before a rename.

A project lists the rest in queuekeeper.json's `spelling`, each mapped to its reason; file names are
held to the same exemptions as text:

- `skip` for paths of recorded observations or retained upstream text (a key ending in `/` is a
  directory). A skipped path keeps its own name as well as its contents, and the skip key's text is
  exempt wherever it appears, so its entry and links to it spell the kept name; the directories
  above it are still judged.
- `exemptPhrases` for a standard's terms.
- `exemptRuns` for whole identifiers another project named.
- `exemptTokens` for proper names and foreign words.

A bare case of another project's type cannot be told from a project's own name, so mark it:
`case .canceled:  // spelling: keep, URLError.Code`.

**Fix a hit by spelling the word the American way; never widen a pattern to make one go away.** An
American word that a pattern mistakes for British, such as valise or hatred, is no project's
exemption: it joins the checker's American words, with a sample in its controls. Proper names and
foreign words with British endings stay with each project, in `exemptTokens`, since the project
knows why its fixtures hold them; the toolkit ships no shared list.

### The command's old name

The same check fails text that calls the command by its old name, since a branch written before
the rename to `qk` on 2026-11-12, or a wrapped line a rename missed, would tell a reader to type the
package's name (vocaro/queuekeeper#35). Fix a hit by writing `qk`.

- A hit is that name, lowercase and a word of its own, followed by a subcommand, and reads
  `PATH:LINE: the command's old name 'OLD COMMAND' -> qk COMMAND`.
- The subcommand names are the command line's own parser's as that run built it, a loaded
  extension's and an enabled visibility feature's included, so a new subcommand is covered.
- It reads through what a wrapped line puts between the two: spaces and line breaks, a string's
  `\n`, the quotes ending one string literal and opening the next, a backslash continuing a line,
  and a comment's leader at the start of the next line.
- What keeps the name is no hit: the capitalized product, a path or file name such as
  `queuekeeper.json`, the `queuekeeper:` that begins an error, and the prefix of a line a subcommand
  prints, its name and then a colon, as in the `queuekeeper unittest: N tests ...` summary the merge
  queue parses. Mark anything else kept on purpose `spelling: keep` with its reason, or list it in
  `exemptPhrases`.

## Docs check

`python3 scripts/qk docs check [PATHS]` checks a project's Markdown: each link's file, anchor and
source line (`PATH:LINE`) must exist, and an `AGENTS.md` must stay under 32 KiB. With no paths it
reads the Markdown at the root and under `doc/`, `docs/`, `guides/`, `knowledge/`, `agents/` and
`policies/`. The whole check of this checkout takes under a second, well inside the docs lane's limit.

**Commands a reader types.** Each `qk WORD` in a fenced shell block (`sh`, `bash`, `zsh`, `shell`,
`console` or none) or in a code span, by any path to the command and with `--project` before the word
if given, must name a subcommand: one of the core's, the project's extensions', or any plugin's or
visibility feature's in this checkout, whether or not it is loaded or on. A hit reads `PATH:LINE:
names no qk command: qk WORD`. A word in capitals, as in `qk COMMAND`, is a placeholder and not read.
The docs had told the owner to type commands no shell found (vocaro/queuekeeper#39); a test of the
setup script likewise runs the `qk --help` it installs, found on the PATH by name.

**Citations, for a project that turns them on.** With `"docs": {"citations": true}` in
queuekeeper.json, the check also reads citations of a document by its path and a section title, as in
`(doc/guides/admission.md, Heavy-work admission)`, and by its path and an anchor, as in
`doc/guides/process-supervision.md#probing-stop-signals` (vocaro/queuekeeper#114). Queuekeeper turns
it on; a project without the key gets the Markdown checks alone.

- **What it reads.** Code comments, messages and schema descriptions cite the guides, so with no
  paths it reads every tracked Markdown file, source file, script (an extensionless file that starts
  `#!`) and `*.schema.json`, and with paths those named. It reads no other data, logs or plain text,
  and nothing under an `evidence/` or `archive/` folder, which records what a guide said when it was
  written.
- **What must match.** The path is from the checkout's root or from the citing file's folder. The
  title must begin with a heading of that document, or with a bold lead-in that opens one of its
  paragraphs or list items, such as **Landing ahead of the queue.** A citation may go on past the
  title, as in `Two gates and Checkpoints`, and may wrap across the lines of a comment or across
  adjacent string literals.
- **Hits.** A hit reads `PATH:LINE: cites a missing document`, `cites a missing anchor` or
  `cites no heading or bold lead-in of`. Fix the citation, or restore a heading that a rename lost.
- **What is not a citation.** Text after the comma that begins in lower case, names another document,
  or holds braces, brackets, operators or a call before it closes is a list, prose or code. A
  citation of another repository's document, marked by the possessive before its path as in "the
  toolkit's doc/guides/releasing.md, Keeping the split", is not read, since this checkout cannot
  resolve it.

**Why it is each project's choice.** Projects' records and code quote guides their checkouts cannot
resolve: read in every project on 2026-11-13, it failed in nine, mostly on logs and archived text. A
project fixes its own hits before turning it on.

**Why it exists.** When guide sections moved here from the toolkit (vocaro/vocaro-engineering#403),
citations of them went stale with nothing reporting it; its first run here found two more.
