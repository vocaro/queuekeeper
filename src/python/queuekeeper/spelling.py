"""British spellings in a project's text, identifiers included (the toolkit's doc/agents/core.md, Writing).

The words arrive one at a time and are invisible in review, and later sessions copy the spelling
they read, so they are caught mechanically. The checker began as mpm's tree-wide gate.

How a line is read: every run of letters is split at its camelCase, snake_case and SCREAMING_CASE
boundaries, and each subword is judged whole, never as a substring, because
`QuiescentRegionCount` contains "centRe" case-insensitively. A subword is British when it is
  * on the irregular list (licence, grey, whilst, catalogue, judgement, plough, misspelt, ...),
  * a doubled-l inflection of a known stem (travelled, labelling, modeller, counsellor, ...), or
  * a pattern hit (-ise/-yse/-our/-tre/-bre and their inflections) whose stem ends in a letter
    British words put before that ending, and whose uninflected base is not an American word that
    merely looks British (AMERICAN_OK: exercise, valise, glamour, ...).
Detection is by pattern rather than by word list, so a British form nobody anticipated is still
caught; the irregular list covers only what no pattern describes. The letters before an ending
are what keep a name or a foreign word from passing for British: no British word has -ouise or
-jour where American English has -ouize or -jor, so Louise and bonjour pass instead of drawing
the fixes "louize" and "bonjor".

File names are judged the same way, binary files and directories included, because tests, docs
and scripts link to a file by its name and every reference must move when it is renamed. A name
is read like a line, so it splits at `/`, `.`, `-`, `_` and its camelCase boundaries; each
directory is judged once, reported as `dir/`. A content check alone passed `colour-chart.png`,
which has no text, and caught `NeighbourAlignmentTests.swift` only because its class repeated it.

What is exempt, each learned by breaking it:
  * Someone else's name, verbatim: another project's API (Unity's `MonoBehaviour`, Apple's
    `isCancelled`), a file name searched for literally (`LICENCE`). A project adds its own in
    queuekeeper.json: proper names (Denise, Seymour), foreign words (entre, chapitre), a
    standard's terms, retained upstream text. They come from its own fixtures and corpora, so it
    lists each with its reason; the toolkit keeps no shared list of names or foreign words.
  * A cited title. The first <em> of a list item is a bibliography entry's title slot, kept
    verbatim even when the source is British; annotations after it are checked like any prose.
  * URLs, which are addresses rather than prose.
  * A line marked `spelling: keep` and a reason, such as a decoder that still reads a name
    stored before a rename.
  * The directional adverbs towards/backwards/forwards/afterwards: good American English, and a
    sweep that "corrected" one rewrote a cited title into one its authors never published.
  * Words only context can judge: "disc" and the temporal "any more". Review owns those.
  * A skipped path's own name, with its contents: recorded observations and retained upstream
    text keep the names they were given. The directories above it are still judged, and the
    skip entry's text is exempt wherever it appears, as a link to the kept name must spell it.
A hit is fixed by spelling the word the American way. An American word that a pattern mistakes
(valise, troubadour, hatred) joins AMERICAN_OK here, with a sample in the American controls. A
name or foreign word that still trips a pattern (Denise, Cavour, entre, chapitre) goes into the
project's exemptions with its reason. A pattern never gains an exception for one hit: its
letters change only for a British word that has the letter.

The command's old name is caught here too (vocaro/queuekeeper#35). The command is `qk`, and text
that tells a reader to type the package's name before a subcommand sends them to a command that
does not exist; the rename of 2026-11-12 missed eleven such places, and a branch written before it
brings more. So `check` is given the subcommand names of the command line's own parser, which
covers a new subcommand the day it is added, and reports the package's name followed by one of
them. Between the two it reads through what a wrapped line puts there: spaces and line breaks, a
`\n` escape, a closing and an opening quote of one string literal continued in the next, a
backslash that continues a line, and a comment's leader at the start of the next line. What keeps
the name on purpose is no hit: a path or a file name (`queuekeeper.json`, `src/python/queuekeeper`,
`vocaro/queuekeeper#35`), the capitalized product, and the prefix of a line the command prints:
the `queuekeeper:` of an error, and a subcommand's own, which is its name and then a colon, as in
the summaries the merge queue parses (`queuekeeper unittest: 4 tests ...`). No reader types the
colon, so the old name, a subcommand and a colon at once are a prefix and no hit. Whatever else
keeps the name is an `exemptPhrases` entry of the project's or a line marked `spelling: keep`.

Positive controls run first, every time: a detector that has stopped detecting looks exactly like
a clean tree. Exit 0 when clean, 1 when a British spelling or the command's old name is found or a
control fails, and 2 when
the run never happened.
"""
import os
from pathlib import Path
import re
import subprocess

BINARY = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".heic", ".tif", ".tiff", ".ico", ".icns",
          ".mp4", ".mov", ".m4a", ".wav", ".caf", ".aiff", ".mp3", ".exr", ".psd", ".ttf", ".otf",
          ".woff", ".woff2", ".zip", ".gz", ".xz", ".bz2", ".tar", ".epub", ".sqlite", ".car",
          ".meta"}

SUBWORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
URL = re.compile(r"https?://\S+")
# A string escape's letters would otherwise join the word after it: "\nraise" is not "nraise",
# and JSON's "\u201cyour" (an opening quotation mark) is not "cyour".
ESCAPE = re.compile(r"\\(?:[nrtfvb0]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})")
KEEP = re.compile(r"spelling: keep\b\W*\w")

# Pattern families, each matched against a whole lowercase subword. `stem` + the family's base
# ending rebuilds the uninflected word that AMERICAN_OK is checked against.
#
# A stem must end in a letter that British words put before that ending, so that the American
# form a hit suggests is a word: -ise follows anything but w or ou (no American word ends in -wize
# or -ouize: otherwise, Louise; heroise and soliloquise are British); -yse follows only l (analyse);
# -our follows b, c, d, g, i, l, m, n, p or v (harbour, rancour, odour, rigour, behaviour, colour,
# humour, honour, vapour, favour); -tre follows a, c, e, i, l, n, p, s or u (theatre, spectre,
# metre, litre, philtre, centre, sceptre, lustre, accoutre); -bre follows a, i or om (sabre,
# fibre, sombre). The other letters were names, foreign words and text that lost its spaces, and
# each fix suggested was not a word: Louise -> louize, Apryse, Keyser, Hanjour, Wintour, Balfour,
# bonjour, votre, lettres, Chartres, sobre, septiembre, Readwise's, milesperhour, sinceour. A
# letter joins a set only with a British word that has it there; no ending gains an exception for
# one word.
ISE = re.compile(r"(?P<stem>[a-z]{3,}?)(?<!w)(?<!ou)is(?P<suf>e|es|ed|ing|ation|ations|ational|able"
                 r"|er|ers|ement|ements)")
YSE = re.compile(r"(?P<stem>[a-z]+?l)ys(?P<suf>e|es|ed|ing|er|ers)")
OUR = re.compile(r"(?P<stem>[a-z]+?[bcdgilmnpv])our(?P<suf>s|ed|ing|al|able|ably|ite|ites|itism|ful"
                 r"|fully|less|hood|hoods|er|ers|ist|ists|ism|ly|ish|y|ation|ations)?")
TRE = re.compile(r"(?P<stem>[a-z]+?[aceilnpsu])tre(?P<suf>s|d)?")        # me-tre
BRE = re.compile(r"(?P<stem>[a-z]+?(?:[ai]|om))bre(?P<suf>s|d)?")      # fi-bre, som-bre
# The doubled-l inflections, by stem, with the prefixes that keep the doubling.
DOUBLE_L = re.compile(
    r"(?P<stem>(?:re|un|mis|pre)?(?:travel|cancel|model|label|marshal|signal|level|total|tunnel"
    r"|channel|counsel|dial|funnel|fuel|equal|panel|parcel|pedal|quarrel|rival|shovel|snorkel"
    r"|swivel|unravel|revel|jewel|bevel|chisel|cudgel|duel|enamel|gambol|grovel|kennel|libel"
    r"|marvel|pencil|spiral|tassel|towel|trammel|yodel|initial|dishevel|journal))"
    r"l(?P<suf>ed|ing|er|ers|or|ors)")

IRREGULAR = {
    "licence": "license", "licences": "licenses", "licenced": "licensed", "licencing": "licensing",
    "defence": "defense", "defences": "defenses", "defenceless": "defenseless", "offence": "offense", "offences": "offenses",
    "pretence": "pretense", "pretences": "pretenses", "practise": "practice",
    "practises": "practices", "practised": "practiced", "practising": "practicing",
    "grey": "gray", "greys": "grays", "greyed": "grayed", "greying": "graying",
    "greyscale": "grayscale", "greyish": "grayish", "catalogue": "catalog", "catalogues": "catalogs",
    "catalogued": "cataloged", "cataloguing": "cataloging", "analogue": "analog",
    "analogues": "analogs", "judgement": "judgment", "judgements": "judgments",
    "acknowledgement": "acknowledgment", "acknowledgements": "acknowledgments",
    "artefact": "artifact", "artefacts": "artifacts", "encyclopaedia": "encyclopedia",
    "whilst": "while", "amongst": "among", "orientate": "orient", "orientated": "oriented",
    "orientating": "orienting", "manoeuvre": "maneuver", "manoeuvres": "maneuvers",
    "manoeuvred": "maneuvered", "manoeuvring": "maneuvering", "programme": "program",
    "programmes": "programs", "sulphur": "sulfur", "sulphurous": "sulfurous",
    "sulphureous": "sulfureous", "sulphuric": "sulfuric", "sulphate": "sulfate",
    "sulphates": "sulfates", "sulphide": "sulfide", "sulphides": "sulfides", "aluminium": "aluminum", "mould": "mold",
    "moulds": "molds", "moulded": "molded", "moulding": "molding", "storey": "story",
    "storeys": "stories", "tyre": "tire", "tyres": "tires", "kerb": "curb", "draught": "draft",
    "draughts": "drafts", "cheque": "check", "cheques": "checks", "plough": "plow",
    "ploughs": "plows", "ploughed": "plowed", "ploughing": "plowing", "skilful": "skillful",
    "skilfully": "skillfully", "skilfulness": "skillfulness", "wilful": "willful",
    "wilfully": "willfully", "wilfulness": "willfulness", "fulfil": "fulfill", "fulfils": "fulfills", "fulfilment": "fulfillment",
    "instil": "instill", "distil": "distill", "enthral": "enthrall", "instalment": "installment",
    "enrol": "enroll", "enrols": "enrolls", "enrolment": "enrollment",
    "enrolments": "enrollments", "learnt": "learned",
    "misspelt": "misspelled", "dreamt": "dreamed", "spoilt": "spoiled", "ageing": "aging",
    "sceptic": "skeptic", "sceptical": "skeptical", "scepticism": "skepticism", "cosy": "cozy",
    "maths": "math", "aeroplane": "airplane", "speciality": "specialty",
    "specialities": "specialties", "jewellery": "jewelry", "woollen": "woolen",
    "marvellous": "marvelous", "focussed": "focused", "focussing": "focusing",
    "focusses": "focuses", "refocussed": "refocused", "refocusses": "refocuses",
    "refocussing": "refocusing",
    "benefitted": "benefited", "benefitting": "benefiting", "targetted": "targeted",
    "targetting": "targeting", "biassed": "biased", "enquire": "inquire", "enquiry": "inquiry",
    "enquiries": "inquiries", "tranquillity": "tranquility", "sizeable": "sizable",
    "useable": "usable", "moveable": "movable", "likeable": "likable", "liveable": "livable",
    "cognisant": "cognizant", "ochre": "ocher", "ochres": "ochers", "ochreous": "ocherous",
    "ochrey": "ochery", "haemorrhage": "hemorrhage", "haemorrhages": "hemorrhages",
    "haemorrhaged": "hemorrhaged", "haemorrhaging": "hemorrhaging",
    "haemorrhagic": "hemorrhagic", "leukaemia": "leukemia", "titbit": "tidbit",
    "titbits": "tidbits", "routeing": "routing",
}

# American words the patterns mistake for British ones, as UNINFLECTED bases (the base of
# "revised" is "revise"). A word that is no inflection of the base a pattern rebuilds is listed
# whole: "analyses" and "urinalyses" are the American plurals of "analysis" and "urinalysis", not
# British verbs, and "hatred" is no "hatre" + d. A compound whose head PREFIXES does not list has
# its own entry (highrise, fundraise, downpour), as do the CSS named colors (mediumvioletred,
# palevioletred) and a prefixed word whose remainder is too short for its pattern to judge
# (unraised, outpour). "redisable" is re-disable. A word whose stem ends where no British word's
# does (geyser, contour, timbre, thoroughbred) needs no entry.
AMERICAN_OK = frozenset("""
advertise advise arise chastise comprise compromise concise cruise demise despise devise
disguise enterprise excise exercise expertise franchise guise improvise incise merchandise
paradise poise praise precise premise promise raise reprise revise rise sunrise moonrise
supervise surmise surprise televise treatise turquoise tortoise porpoise noise denoise bruise
appraise apprise chaise counterpoise equipoise valise marquise malaise highrise fundraise uprise
enfranchise darkturquoise mediumturquoise paleturquoise unraised microenterprise
analyses urinalyses
our pour flour scour devour dour amour glamour velour
troubadour pompadour paramour downpour outpour overpour
acre cadre genre macabre massacre mediocre ogre lucre padre nacre libre hombre
hatred mediumvioletred palevioletred
redisable
""".split())

# A prefix that leaves an American word American: "unexercised" is un + exercised. It clears
# only a word whose remainder's base is in AMERICAN_OK, so "disorganised" still fails on
# "organised" while "imprecise" is im + precise.
PREFIXES = ("un", "re", "pre", "non", "in", "im", "dis", "over", "under", "mis", "counter", "out",
            "self", "super", "sub", "semi", "multi", "inter")

# Whole identifier runs other projects named the British way: Unity's and Apple's APIs.
EXEMPT_RUNS = frozenset({
    "MonoBehaviour", "MonoBehaviours", "StateMachineBehaviour", "StateMachineBehaviours",
    "isCancelled", "testCancelled", "userCancelled", "paymentCancelled", "operationCancelled",
    "NSUserCancelledError", "NSURLErrorCancelled", "touchesCancelled", "documentPickerWasCancelled",
    "matchmakerViewControllerWasCancelled", "turnBasedMatchmakerViewControllerWasCancelled",
    "reindexAllSearchableItemsWithAcknowledgementHandler", "acknowledgementHandler"})
# Case-sensitive subwords: a file name some projects use and audits search for literally, and
# Vocaro's Pagepour, a product name that ends in "pour", with its plural, which is also its
# possessive once an anchor (#reproduce-from-pagepours-tools) or identifier drops the apostrophe.
EXEMPT_TOKENS = frozenset({"LICENCE", "Pagepour", "pagepour", "PAGEPOUR",
                           "Pagepours", "pagepours", "PAGEPOURS"})


def _family(word):
    """(suggestion, base) when `word` matches a pattern family; None otherwise."""
    m = ISE.fullmatch(word)
    if m:
        return m.group("stem") + "iz" + m.group("suf"), m.group("stem") + "ise"
    m = YSE.fullmatch(word)
    if m:
        return m.group("stem") + "yz" + m.group("suf"), m.group("stem") + "yse"
    m = OUR.fullmatch(word)
    if m:
        return m.group("stem") + "or" + (m.group("suf") or ""), m.group("stem") + "our"
    for pattern, british, american in ((TRE, "tre", "ter"), (BRE, "bre", "ber")):
        m = pattern.fullmatch(word)
        if m:
            suf = {"s": "s", "d": "ed"}.get(m.group("suf") or "", "")
            return m.group("stem") + american + suf, m.group("stem") + british
    return None


def american_for(subword):
    """The American spelling of a British subword, or None when it is not British."""
    w = subword.lower()
    if w in IRREGULAR:
        return IRREGULAR[w]
    m = DOUBLE_L.fullmatch(w)
    if m:
        return m.group("stem") + m.group("suf")
    hit = _family(w)
    if hit is None or w in AMERICAN_OK or hit[1] in AMERICAN_OK:
        return None
    for p in PREFIXES:                        # un + exercised: judge what follows the prefix
        if w.startswith(p) and len(w) > len(p) + 3:
            rest = _family(w[len(p):])
            if rest is not None and rest[1] in AMERICAN_OK:
                return None
    return hit[0]


# The command's old name and its new one.
OLD_COMMAND, COMMAND = "queuekeeper", "qk"
# What may stand between the old name and a subcommand where a reader would type one space: white
# space, a string's \n, \r or \t, a backslash continuing a line, the quote that closes one string
# literal and the quote that opens the next, and a comment's leader at the start of a line. A lone
# quote or backtick is not among them: "the `queuekeeper` queue" names the package.
BETWEEN = r"""(?:\s|\\[nrt]|\\(?=\n)|["'][\s+\\]*[fFrRbBuU]{0,2}["']|^[ \t]*(?:\#|//|\*|>|--(?=\s)))+"""
SPACED = re.compile(r"\s|\\[nrt]")


def old_command_pattern(commands):
    """The pattern that finds OLD_COMMAND before one of the subcommand names, or None with no names.
    The old name begins a word, which a string's \\n before it does not prevent: that one the rename
    missed. --project and its value may come before the subcommand, as on the command line. A colon
    straight after the subcommand makes the two a printed line's prefix, which keeps the name: the
    merge queue and `qk progress` parse "queuekeeper unittest: 4 tests ..." by it."""
    names = sorted({name for name in commands if name}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(
        rf"(?:(?<![\w./$-])|(?<=\\[nrt])){OLD_COMMAND}(?P<between>{BETWEEN})(?:--project(?:=|\s+)\S+\s+)?"
        rf"(?P<name>{'|'.join(map(re.escape, names))})(?![\w:-]|\.\w)", re.M)


class Rules:
    """The built-in exemptions plus a project's own from queuekeeper.json's `spelling`, and the
    subcommand names that the command's old name may not precede."""

    def __init__(self, project=None, commands=()):
        project = project or {}
        self.old_command = old_command_pattern(commands)
        self.skip = tuple(project.get("skip", {}))
        # A skipped path keeps its name, so its skip entry and a link to it spell it as kept.
        self.phrases = tuple(sorted({*project.get("exemptPhrases", {}), *self.skip}, key=len,
                                    reverse=True))
        self.runs = EXEMPT_RUNS | set(project.get("exemptRuns", {}))
        self.tokens = EXEMPT_TOKENS | set(project.get("exemptTokens", {}))

    def skips(self, path):
        return any(path == s or (s.endswith("/") and path.startswith(s)) for s in self.skip)

    def british_in(self, line):
        """[(subword, american)] for every British subword on one line of text."""
        if KEEP.search(line):
            return []
        line = ESCAPE.sub(" ", URL.sub(" ", line))
        for phrase in self.phrases:
            line = line.replace(phrase, " ")
        found = []
        for m in re.finditer(r"[A-Za-z]+", line):
            run = m.group(0)
            if run in self.runs:
                continue
            for sub in SUBWORD.findall(run):
                if sub in self.tokens:
                    continue
                american = american_for(sub)
                if american:
                    found.append((sub, american))
        return found

    def old_commands_in(self, text):
        """[(line, subcommand)] for each place the text calls the command by its old name."""
        if self.old_command is None or OLD_COMMAND not in text:
            return []
        for phrase in self.phrases:              # the same length, so that a match keeps its line
            text = text.replace(phrase, "\0" * len(phrase))
        found = []
        for m in self.old_command.finditer(text):
            if not SPACED.search(m.group("between")):
                continue
            end = text.find("\n", m.end())
            lines = text[text.rfind("\n", 0, m.start()) + 1:len(text) if end < 0 else end]
            if not any(KEEP.search(line) for line in lines.split("\n")):
                found.append((text.count("\n", 0, m.start()) + 1, m.group("name")))
        return found


BRITISH_SAMPLES = r"""colour Colour COLOUR colourRamp NEIGHBOUR_OFFSETS HarbourBreachStage
    normalised Normalisation renormalised unnormalised analyse centre centres Centred fibre licence
    defence grey whilst catalogue analogue travelled labelling relabelled modeller counsellor
    bevelled unfavourable unrecognised behaviour misspelt practise kilometres
    BehaviourBoundError licence_display wasCancelled \ncolour \u201ccolour organise
    theatre paralyse odour rigour rancour vapour sceptre spectre lustre philtre accoutre sabre
    sombre armoury colouration favouritism aggrandisement cognisant journalling refocussed
    skilfully defenceless sulphuric ochre haemorrhage leukaemia titbit routeing enrolments
    """.split()
AMERICAN_SAMPLES = r"""QuiescentRegionCount MonoBehaviour aria-labelledby towards afterwards exercise
    unexercised advertised revised surprising unsurprising contour hours yours Fourier genre acres
    timbre controlled installer cancellation distilled fulfilled nodewise pairwise otherwise
    LICENCE disc redisable analyses center color behavior normalize harbor isCancelled
    userCancelled LibreSSL Pagepour Pagepour's pagepours Pagepours \nraise sunrise
    https://example.com/colour-grading geyser
    geysers urinalyses chaises counterpoises equipoise valise marquise malaise troubadours
    pompadour paramour imprecise inadvisable disfranchise dispraise highrise downpours fundraising
    darkturquoise mediumturquoise paleturquoise uprising enfranchised disenfranchised unraised
    outpouring overpouring hatred thoroughbred underbred mediumvioletred palevioletred
    microenterprise microenterprises eaa-microenterprise Microenterprise hombres Apryse Keyser
    Louise Readwises bonjour Hanjour Balfour Wintour votre lettres Chartres sobre septiembre
    milesperhour sinceour \u201cyour advertisement chastisement disenfranchisement journaling
    hourly dialogue doughnut saleable writeable lambast""".split()
# Tracked paths: a binary file's name, a directory's, and a file name split at camelCase.
BRITISH_PATHS = ("Docs/colour-chart.png", "centre/README.md", "Tests/NeighbourAlignmentTests.swift",
                 "Fonts/grey_sans.ttf")
AMERICAN_PATHS = ("LICENCE", "Sources/QuiescentRegionCount.swift", "Unity/MonoBehaviour.cs",
                  "center/color-chart.png", "docs/eaa-microenterprise.md",
                  "Sources/Microenterprise.swift")


def names_of(paths, rules):
    """Every file and directory name the paths hold, each once and a directory as `dir/`, with
    skipped paths' own names left out."""
    held = set(paths)
    for path in paths:
        parts = path.split("/")
        held.update("/".join(parts[:i]) + "/" for i in range(1, len(parts)))
    return sorted(name for name in held if not rules.skips(name))


def british_names(names, rules):
    """[(name, subword, american)] for every British subword in each name's last component."""
    return [(name, sub, american) for name in names
            for sub, american in rules.british_in(name.rstrip("/").rsplit("/", 1)[-1])]


# The command's old name before a subcommand, in each shape a line takes, and the uses that keep the
# name. They are assembled, so that no file but this one holds the old name before a subcommand.
CONTROL_COMMANDS = ("run", "admissions", "pre-push", "unittest", "spelling", "queue")
OLD_COMMAND_SAMPLES = tuple(sample.replace("@", OLD_COMMAND) for sample in (
    "Run `@ run gate`.", "@ admissions usage", "then @\nqueue status", 'print("@ "\n      "admissions usage")',
    "say('@ ' +\n    f'run {lane}')", 'error = "refused\\n@ run gate"', '"@\\nrun"', "@ \\\n  pre-push origin",
    "# with @\n    # run gate", "// @\n// queue status", "@ --project ../app run gate", "@ unittest -s tests",
    "@ run gate: passed"))
KEPT_NAME_SAMPLES = tuple(sample.replace("@", OLD_COMMAND) for sample in (
    "@.json's queue", "src/python/@ run", "vocaro/@ queue", "@: run refused", "@ unittest: 4 tests (0 skipped)",
    "@ spelling: PASS", "@ pre-push: refused", "the `@` queue", '("@", "run")', "@_unity run", "@ runs", "@ queued",
    "@ queue.lanes", "import @\nrun = 1  # spelling: keep, a module and a variable", "@-run", "@ pre-pushed")
    ) + ("Queuekeeper run", "QUEUEKEEPER run", "qk run gate")


def controls():
    """Failures of the built-in positive controls; empty when the detector still detects."""
    rules = Rules(commands=CONTROL_COMMANDS)
    failures = [f"the command's old name was not flagged in {s!r}" for s in OLD_COMMAND_SAMPLES
                if not rules.old_commands_in(s)]
    failures += [f"{s!r} was flagged as the command's old name" for s in KEPT_NAME_SAMPLES
                 if rules.old_commands_in(s)]
    return failures + _spelling_controls()


def _spelling_controls():
    rules = Rules()
    failures = [f"'{s}' was not flagged" for s in BRITISH_SAMPLES if not rules.british_in(s)]
    failures += [f"'{s}' was flagged as {rules.british_in(s)}" for s in AMERICAN_SAMPLES
                 if rules.british_in(s)]
    flagged = {s: british_names(names_of([s], rules), rules) for s in BRITISH_PATHS + AMERICAN_PATHS}
    failures += [f"file name '{s}' was not flagged" for s in BRITISH_PATHS if not flagged[s]]
    failures += [f"file name '{s}' was flagged as {flagged[s]}" for s in AMERICAN_PATHS if flagged[s]]
    return failures


LIST_ITEM = re.compile(r"<li>.*?</li>", re.S)
FIRST_EM = re.compile(r"<em>.*?</em>", re.S)


def problems_in(text, rules):
    """[(line, subword, american)] in one file's text, cited titles aside."""
    # The first <em> of each <li> is a bibliography entry's verbatim title slot. Blank only the
    # title, keeping its line breaks, so an annotation on the same line is still read.
    if "<li>" in text:
        titles = [em.span() for li in LIST_ITEM.finditer(text)
                  for em in [FIRST_EM.search(text, li.start(), li.end())] if em]
        for start, end in reversed(titles):
            text = text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]
    return [(i, sub, american) for i, line in enumerate(text.split("\n"), 1)
            for sub, american in rules.british_in(line)]


def check(root, config, paths=(), out=print, commands=()):
    """Report every British spelling in the project's file names and text, and every place its text
    calls the command by its old name before one of commands, the command line's subcommand names;
    return the exit status.

    With no paths it reads what a push carries: tracked files. Named paths, relative to the
    current directory, also read untracked files Git does not ignore, so a page about to be
    published from disk is checked before it is committed.
    """
    root = Path(root).resolve()
    failures = controls()
    if failures:
        for failure in failures:
            out(f"queuekeeper spelling: CONTROL FAILED — {failure}")
        out("queuekeeper spelling: FAIL — the detector itself is wrong; no file result can be trusted")
        return 1
    rules = Rules(config.get("spelling"), commands)
    named = []
    for path in paths:
        relative = os.path.relpath(Path(path).resolve(), root)
        if relative == ".." or relative.startswith("../"):
            out(f"queuekeeper spelling: {path} is outside {root}; nothing was checked.")
            return 2
        named.append(relative)
    untracked = ["--others", "--exclude-standard"] if named else []
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", *untracked,
                                 "--", *named], check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        out(f"queuekeeper spelling: cannot list tracked files ({error}); nothing was checked.")
        return 2
    this = Path(__file__).resolve()
    tracked = sorted({f for f in os.fsdecode(listed.stdout).split("\0") if f})
    names = names_of(tracked, rules)
    files = [f for f in tracked if not rules.skips(f) and os.path.splitext(f)[1].lower() not in BINARY
             and (root / f).is_file() and (root / f).resolve() != this]
    if named and not names:
        out(f"queuekeeper spelling: no files under {' '.join(paths)}; nothing was checked.")
        return 2
    found = old = 0
    for name, sub, american in british_names(names, rules):
        out(f"{name}: British spelling in file name '{sub}' -> {american}")
        found += 1
    for f in files:
        raw = (root / f).read_bytes()
        if b"\0" in raw[:8192]:
            continue                                  # binary content under a text-looking name
        text = raw.decode("utf-8", "replace")
        for line, sub, american in problems_in(text, rules):
            out(f"{f}:{line}: British spelling '{sub}' -> {american}")
            found += 1
        for line, name in rules.old_commands_in(text):
            out(f"{f}:{line}: the command's old name '{OLD_COMMAND} {name}' -> {COMMAND} {name}")
            old += 1
    renamed = (f"{old} use(s) of the command's old name: the command is {COMMAND}, and a line that keeps the old name "
               "on purpose is marked `spelling: keep` with its reason (doc/guides/maintenance.md, Spelling check)")
    if found:
        out(f"queuekeeper spelling: FAIL — {found} British spelling(s) in {len(files)} text file(s) and "
            f"{len(names)} file name(s). Spell them the American way (the toolkit's doc/agents/core.md, Writing)."
            + (f" Also {renamed}." if old else ""))
        return 1
    if old:
        out(f"queuekeeper spelling: FAIL — {renamed}; {len(files)} text file(s) read.")
        return 1
    out(f"queuekeeper spelling: PASS — {len(files)} text file(s) and {len(names)} file name(s), identifiers "
        f"split at their word boundaries, {len(BRITISH_SAMPLES)}/{len(AMERICAN_SAMPLES)} British/"
        f"American word and {len(BRITISH_PATHS)}/{len(AMERICAN_PATHS)} file name controls held"
        + (f"; the command's old name precedes none of {len(set(commands))} subcommand name(s)"
           if rules.old_command else ""))
    return 0
