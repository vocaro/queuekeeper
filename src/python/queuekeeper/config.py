"""Small, strict project configuration without third-party dependencies."""
import importlib.util
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from . import extensions as hooks, pins, sandbox

TOOLKIT_ROOT = Path(__file__).resolve().parents[3]
# The extensions a project gets when its queuekeeper.json names none, and that a command with no project loads.
EXTENSIONS = ()
FILE = "queuekeeper.json"


def extensions(declared):
    """A project's extensions, checked: each a module's dotted name, or {"module": NAME, "path": FOLDER} for one
    whose package is in FOLDER, a path relative to the project's main checkout."""
    if not isinstance(declared, list): raise ValueError("extensions must be a list")
    normalized = []
    for entry in declared:
        named = entry.get("module") if isinstance(entry, dict) else entry
        if not isinstance(named, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", named) or (
                isinstance(entry, dict) and (set(entry) - {"module", "path"} or not isinstance(entry.get("path", ""), str))):
            raise ValueError('extensions lists module names, or {"module": NAME, "path": FOLDER}')
        unknown(named)
        normalized.append(dict(entry) if isinstance(entry, dict) else named)
    return normalized


def unknown(named):
    """Refuse an extension named as a module of Queuekeeper's own package that it does not have, such as
    queuekeeper.apple: the plugins are packages of their own, named with an underscore (vocaro/queuekeeper#187).
    A folder with no module in it, such as one a checkout keeps from before the plugins left the core with only
    ignored files in it, imports as a namespace package, which has no origin, and is no module either."""
    if not named.startswith(__package__ + "."): return
    try: found = getattr(importlib.util.find_spec(named), "origin", None) is not None
    except ModuleNotFoundError: found = False
    if found: return
    plugin = __package__ + "_" + named.removeprefix(__package__ + ".")
    raise ValueError(f"extensions names {named}, which is no module of Queuekeeper's"
                     + (f"; the plugin is the package {plugin}: list {plugin}" if "." not in plugin and
                        importlib.util.find_spec(plugin) is not None else ""))


def extensions_of(root, base=None):
    """What the project at root switches on, as (module, folder or None) pairs: its queuekeeper.json's extensions,
    or EXTENSIONS when it names none or there is no readable project. A folder is resolved against base, the
    project's main checkout when root is a linked worktree of it, else against root; but a folder beside the project
    that the run this process belongs to pinned (pins.held) is that run's checkout of it, so that every command of
    a run loads what the run started with (vocaro-engineering#497). A pin set that is not what it says is
    pins.Unpinned."""
    try: declared = extensions(json.loads((Path(root) / FILE).read_text()).get("extensions", list(EXTENSIONS)))
    except (OSError, ValueError, AttributeError): declared = list(EXTENSIONS)
    pinned = pins.held()
    pins.verify({name: pinned[name] for name in sibling_folders(root) if name in pinned} if pinned else None)
    return [(entry["module"], (pins.folder(entry["path"], pinned) or str((Path(base or root) / entry["path"]).resolve()))
             if entry.get("path") else None)
            if isinstance(entry, dict) else (entry, None) for entry in declared]


def sibling_folders(root):
    """The folders beside the project at root that a run of it loads from, by name (sibling_variables)."""
    return list(sibling_variables(root))


def sibling_variables(root):
    """The folders beside the project at root that a run of it loads from, {NAME: VARIABLE or None}: those its
    queuekeeper.json declares (siblings_in), and those each of them declares in turn under "siblings" in its own
    queuekeeper.json, as that folder's checkout beside the project stands. A run pins each at a commit (pins), which
    reads a pinned folder's own list at that commit, and another Mac is not sent them: it takes that commit from
    its own checkout of each (offload_host.place_siblings)."""
    try: found = siblings_in(json.loads((Path(root) / FILE).read_text()))
    except (OSError, ValueError, AttributeError): return {}
    waiting = list(found)
    while waiting:
        for name, variable in further_siblings(pins.clone(root, waiting.pop(0))).items():
            if name not in found: waiting.append(name)
            if not found.get(name): found[name] = variable
    return found


def siblings_in(configuration):
    """The folders beside a project that its configuration declares, {NAME: VARIABLE or None}, as the merge queue
    reads them from main's commit: NAME for each extension folder "../NAME/...", and each NAME of "siblings", a folder
    the project's own code loads from, found by the environment variable VARIABLE, which a run that pins the folder
    sets to its checkout of it (pins.exported). A ValueError says what is wrong with either."""
    found = {}
    for entry in extensions(configuration.get("extensions", [])):
        parts = Path(entry.get("path") or "").parts if isinstance(entry, dict) else ()
        if len(parts) > 1 and parts[0] == ".." and parts[1] not in ("..", "."): found.setdefault(parts[1], None)
    found.update(siblings(configuration.get("siblings", {})))
    return found


def siblings(declared):
    """A configuration's "siblings", checked: {NAME: VARIABLE}, a folder's name beside the project and the
    environment variable its code finds that folder by."""
    if not isinstance(declared, dict) or not all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) and isinstance(variable, str)
            and re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable) for name, variable in declared.items()):
        raise ValueError('siblings is {FOLDER: VARIABLE}: a folder beside the project that its code loads from, and '
                         "the environment variable it finds the folder by")
    return dict(declared)


def further_siblings(folder):
    """What the checkout at folder, itself a folder beside a project, declares under "siblings" in its own
    queuekeeper.json: the folders its code loads from when a project loads it. None where it has no such list."""
    try: return siblings(json.loads((Path(folder) / FILE).read_text()).get("siblings", {}))
    except (OSError, ValueError, AttributeError): return {}


def load(root):
    return parse((Path(root).resolve() / FILE).read_text())


# The keys a lane and a lane group take, which parse checks and schemas/queuekeeper.schema.json lists (a test
# compares them, vocaro-engineering#248).
LANE_KEYS = frozenset({"command", "resource", "minimumTests", "timeout", "foreground", "here", "model", "display",
                       "budgetMinutes", "wholeHost", "apps", "unlockedScreen", "devices", "compileFirst",
                       "deviceRunMinutes", "stall", "unity", "chips", "arguments", "idioms", "exactTests",
                       "failedTests", "labels", "sandbox"})
# The lane keys above that an extension shipped here adds (hooks.LANE_KEYS), by its module: a project that uses one
# without the extension is told which to switch on.
EXTENSION_LANE_KEYS = {"display": "queuekeeper_mac_desktop", "unity": "queuekeeper_unity",
                       "chips": "queuekeeper_mac_hardware", "devices": "queuekeeper_mobile",
                       "deviceRunMinutes": "queuekeeper_mobile", "idioms": "queuekeeper_apple"}
# The top-level keys an extension shipped here adds (hooks.CONFIG_KEYS), by its module, refused the same way.
EXTENSION_CONFIG_KEYS = {"unity": "queuekeeper_unity", "app": "queuekeeper_apple", "simulatorPool": "queuekeeper_apple",
                         "android": "queuekeeper_android", "emulatorPool": "queuekeeper_android"}
GROUP_KEYS = frozenset({"parallel", "jobs", "resource", "here", "model", "budgetMinutes", "wholeHost", "unlockedScreen",
                        "labels", "failFast"})




def lane_devices(lane):
    """Reservations interpreted by the active device provider."""
    return hooks.DEVICES.lane_devices(lane)

def parse(source):
    """A project's queuekeeper.json, checked, from its text, wherever that came from."""
    value = json.loads(source)
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("queuekeeper.json requires schemaVersion 1")
    allowed = {"schemaVersion", "name", "extensions", "lanes", "integration", "repository",
               "queue", "release", "testLanes", "gitHooks", "docs", "siblings"}
    if "spelling" in value: raise ValueError(f"spelling {SPELLING_GONE}")
    if set(value) - allowed - set(EXTENSION_CONFIG_KEYS):
        raise ValueError(f"Unknown configuration keys: {sorted(set(value) - allowed - set(EXTENSION_CONFIG_KEYS))}")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("Project name must be a nonempty string")
    declared = extensions(value.get("extensions", list(EXTENSIONS)))
    if "extensions" in value: value["extensions"] = declared
    siblings(value.get("siblings", {}))
    # The extensions shipped here that the project switches on add keys to its lanes, so they are loaded before the
    # lanes are checked, whichever command reads the configuration. One in a folder of the project's is the
    # command's to load (cli.perform), which knows the folder.
    hooks.load([named for named in declared if isinstance(named, str) and (named.startswith(__package__ + ".") or named in set(EXTENSION_CONFIG_KEYS.values()) | set(EXTENSION_LANE_KEYS.values()))])
    for key, module in EXTENSION_CONFIG_KEYS.items():
        if key not in value: continue
        if key not in hooks.CONFIG_KEYS:
            raise ValueError(f"{key} is the {module} extension's key, and the project does not switch that extension "
                             f"on: list {module} in extensions")
        hooks.CONFIG_KEYS[key](value[key])
    git_hooks(value.get("gitHooks"))
    if value.get("integration", "direct-main") not in {"direct-main", "pull-request"}:
        raise ValueError("Unknown integration policy")
    if not isinstance(value.get("lanes", {}), dict):
        raise ValueError("lanes must be an object")
    lanes = value.get("lanes", {})
    retired(lanes)
    for name, lane in lanes.items():
        budget(name, lane)
        if isinstance(lane, dict) and "parallel" in lane:
            group(name, lane, lanes)
            continue
        if not isinstance(lane, dict) or set(lane) - LANE_KEYS - set(hooks.LANE_KEYS):
            raise ValueError(f"Invalid lane {name}")
        apps(name, lane)
        for key in ["foreground", "model", "wholeHost", "unlockedScreen", "arguments", "exactTests"]:
            if key in lane and type(lane[key]) is not bool:
                raise ValueError(f"{name}.{key} must be true or false")
        resource_of(name, lane)
        if lane.get("foreground") and unadmitted(lane):
            raise ValueError(f"{name}.foreground needs an admitted lane: another Mac's foreground is held inside the "
                             "run's admission, and a self lane is not admitted")
        place(name, lane)
        labels(name, lane)
        model(name, lane)
        whole_host(name, lane)
        screen(name, lane)
        compile_first(name, lane)
        stall(name, lane)
        failed_tests_setting(name, lane)
        sandbox.check(name, lane)
        extension_keys(name, lane)
        command = lane.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f"Lane {name} needs an argv list, never shell source")
        for key in ["minimumTests", "timeout"]:
            if key in lane and (type(lane[key]) is not int or lane[key] <= 0):
                raise ValueError(f"{name}.{key} must be a positive integer")
        if lane.get("exactTests") and "minimumTests" not in lane:
            raise ValueError(f"{name}.exactTests needs the lane's minimumTests, the count it makes exact")
    merge_queue(value.get("queue", {}), lanes)
    release(value.get("release", {}), lanes, value.get("queue", {}))
    test_lanes(value.get("testLanes", {}))
    documentation(value.get("docs", {}))
    return value


def documentation(docs):
    """docs, the settings of qk docs check: citations, true to have it check citations of a document by path and
    section title or anchor in the project's Markdown and source files too (vocaro/queuekeeper#114). Off unless
    set, since a project's records and data quote other repositories' guides that this checkout cannot resolve."""
    if not isinstance(docs, dict) or set(docs) - {"citations"}:
        raise ValueError("docs supports only citations")
    if "citations" in docs and not isinstance(docs["citations"], bool):
        raise ValueError("docs.citations must be true or false")

def git_hooks(folder):
    """gitHooks, the folder of the project's tracked hooks, which qk doctor sets core.hooksPath to where it is unset
    (git_hooks.py, vocaro/queuekeeper#131): a path inside the checkout, relative to its top."""
    if folder is None: return
    if not isinstance(folder, str) or not folder.strip() or folder != folder.strip() or Path(folder).is_absolute() \
            or ".." in Path(folder).parts or Path(folder) == Path("."):
        raise ValueError("gitHooks must be the folder of the project's tracked hooks, relative to the checkout's top, "
                         "such as \".githooks\"")


def stall(name, lane):
    """Quiet monitoring is opt-in on the admitted work, never on a polling/group wrapper."""
    if "stall" not in lane: return
    settings = lane["stall"]
    if not isinstance(settings, dict) or set(settings) - {"seconds", "action", "reason", "primaryControl"}:
        raise ValueError(f"{name}.stall supports seconds, action, reason and primaryControl")
    if "primaryControl" in settings and type(settings["primaryControl"]) is not bool:
        raise ValueError(f"{name}.stall.primaryControl must be a boolean declaring the primary process only coordinates work")
    if type(settings.get("seconds")) is not int or settings["seconds"] <= 0:
        raise ValueError(f"{name}.stall.seconds must be a positive integer")
    if settings.get("action", "report") not in ("report", "stop"):
        raise ValueError(f"{name}.stall.action must be report or stop")
    if "reason" in settings and (not isinstance(settings["reason"], str) or not settings["reason"].strip()):
        raise ValueError(f"{name}.stall.reason must describe the quiet-interval measurement")
    if settings.get("action") == "stop" and not settings.get("reason"):
        raise ValueError(f"{name}.stall stop needs a reason naming the healthy-run measurement and bound")
    if unadmitted(lane):
        raise ValueError(f"{name}.stall needs an admitted leaf lane, not a self lane's polling wrapper")


def failed_tests_setting(name, lane):
    """A lane's failedTests: where the lane writes its results in JUnit XML, under its results folder
    (QUEUEKEEPER_RESULTS_DIR), the command that reruns one of its tests alone, with {test} for the test's name, and the
    paths its tests depend on, each a file or a directory ending in /, which the merge queue reads to rerun a failed
    test alone before a bisect, as it does a qk unittest lane's (vocaro/queuekeeper#109)."""
    if "failedTests" not in lane: return
    settings = lane["failedTests"]
    if not isinstance(settings, dict) or set(settings) - {"results", "rerun", "paths"} \
            or not {"results", "rerun"} <= set(settings):
        raise ValueError(f"{name}.failedTests needs results and rerun, and supports paths")
    results = settings["results"]
    if not isinstance(results, str) or not results.strip() or Path(results).is_absolute() or ".." in Path(results).parts:
        raise ValueError(f"{name}.failedTests.results is the JUnit XML file's path in the lane's results folder "
                         "(QUEUEKEEPER_RESULTS_DIR), relative and inside it")
    rerun = settings["rerun"]
    if not isinstance(rerun, list) or not rerun or any(not isinstance(word, str) or not word for word in rerun) \
            or not any("{test}" in word for word in rerun):
        raise ValueError(f"{name}.failedTests.rerun is an argv list, never shell source, that names {{test}}, the "
                         "failed test it reruns alone")
    paths = settings.get("paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) or not path.strip() for path in paths):
        raise ValueError(f"{name}.failedTests.paths lists the files its tests depend on, each a file or a directory "
                         "ending in /")


def test_lanes(declared):
    """What qk doctor's lane coverage (lane_coverage.py, #289) cannot work out for itself: skip, the test paths kept as
    records, each with its reason, and selects, for a lane whose command it cannot read, the paths the lane's tests
    are under, none for a lane that runs no test."""
    if not isinstance(declared, dict) or set(declared) - {"skip", "selects"}:
        raise ValueError("testLanes supports only skip and selects")
    skip, selects = declared.get("skip", {}), declared.get("selects", {})
    if not isinstance(skip, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(r, str) or not r.strip()
                                         for k, r in skip.items()):
        raise ValueError("testLanes.skip maps each path to the reason it is kept")
    if not isinstance(selects, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(paths, list)
                                            or any(not isinstance(p, str) or not p.strip() for p in paths)
                                            for k, paths in selects.items()):
        raise ValueError("testLanes.selects maps each lane to a list of the paths its tests are under")


def compile_first(name, lane):
    """A lane's "compileFirst", the argv that compiles its test target before its command, in its one admission and
    on the derived data the command uses, so that a compile error stops the run before its tests take a simulator.
    A self lane has no admission of its own to run it in."""
    if "compileFirst" not in lane: return
    command = lane["compileFirst"]
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError(f"{name}.compileFirst needs an argv list, never shell source")
    if unadmitted(lane):
        raise ValueError(f"{name}.compileFirst needs an admitted lane: it runs inside the lane's one admission, and a "
                         "self lane is not admitted")


# What a lane takes of its Mac. Every lane is admitted, first come, first served, and offered to another Mac
# first (the owner's decision, 2026-11-07, vocaro-engineering#200): heavy, one slot of heavy-work admission, which a
# lane that gives no resource takes too; light, which once ran unadmitted where it started, now means heavy, so
# that the queuekeeper.json files that give it keep loading; exclusive, the host to itself; and self, a lane whose
# script admits each of its parts itself, such as a calibration that builds under `qk admit` and then times under
# `qk admit --exclusive`, which an admission of its own would refuse, so qk run runs it where it starts, unadmitted.
RESOURCES = ["light", "heavy", "exclusive", "self"]
# How heavily a lane's admission holds its Mac, so that a group's is at least each of its lanes'.
WEIGHT = {"heavy": 0, "exclusive": 1}


def resource_of(name, lane):
    """The admission the lane or group takes: heavy, exclusive or self, light and no resource meaning heavy."""
    resource = lane.get("resource", "heavy")
    if resource not in RESOURCES:
        raise ValueError(f"Invalid resource for {name}: {resource!r} is none of {', '.join(RESOURCES)}")
    return "heavy" if resource == "light" else resource


# A call of qk admit, as a shell script or a command's own words write it (`scripts/qk admit`), or as a
# Python list of a command's words does (`[*queuekeeper, "admit", ...]`).
ADMIT_CALL = re.compile(r"""\b(?:qk|queuekeeper)\b["' ,\])]*admit\b|["']admit["']""")


def self_lane_warnings(root, config):
    """qk doctor's warning for each self lane whose command neither calls qk admit in its own words nor runs
    a file of the checkout that does: a self lane runs unadmitted, trusted to admit each of its parts, and one
    that admits none runs its work outside admission (owner's decision, 2026-11-07, vocaro-engineering#200).
    The check reads the text and cannot prove that every part is admitted; it catches a lane that admits nothing."""
    warnings, root = [], Path(root)
    for name, lane in sorted(config.get("lanes", {}).items()):
        if not unadmitted(lane) or not isinstance(lane.get("command"), list): continue
        words = [str(word) for word in lane["command"]]
        if ADMIT_CALL.search(" ".join(words)): continue
        # A file of the checkout: an interpreter by its absolute path, such as /opt/homebrew/bin/python3, is none.
        files = [root / word for word in words if not word.startswith("-") and not Path(word).is_absolute()
                 and (root / word).is_file()]
        texts = []
        for path in files:
            try: texts.append(path.read_text(errors="replace"))
            except OSError: pass
        if any(ADMIT_CALL.search(text) for text in texts): continue
        ran = f"the file it runs, {', '.join(path.relative_to(root).as_posix() for path in files)}," if files \
            else "any file of the checkout it runs"
        warnings.append(f"{name} is a self lane, which queuekeeper runs unadmitted, trusting it to admit each of its parts, "
                        f"but neither its command nor {ran} calls qk admit, so its work runs outside admission: "
                        "give it \"resource\": \"heavy\", or admit each part with qk admit")
    return warnings


def extension_keys(name, lane):
    """Check each key of a lane that an extension adds (hooks.LANE_KEYS), by the extension's own rule. A key of
    EXTENSION_LANE_KEYS whose extension the project has not switched on is refused, naming the extension."""
    for key in lane:
        if key in hooks.LANE_KEYS: hooks.LANE_KEYS[key].check(name, lane)
        elif key in EXTENSION_LANE_KEYS:
            raise ValueError(f"{name}.{key} is the {EXTENSION_LANE_KEYS[key]} extension's key, and the project does not "
                             f"switch that extension on: list {EXTENSION_LANE_KEYS[key]}, or an extension that requires "
                             "it, in extensions")


def unadmitted(lane):
    """Whether queuekeeper runs the lane, or group, without admission of its own: a self lane, whose script admits each of
    its parts."""
    return lane.get("resource") == "self"


def admission(lane):
    """The admission qk run takes for the lane, or group: heavy, exclusive, or self for none."""
    return resource_of("the lane", lane)


def exclusive_refusal(config, command):
    """Why `qk admit --exclusive` or `qk remote admit --exclusive` refuses command, or None.

    A command word for word a heavy lane's own (its command or compileFirst) is that lane's work, which its
    configuration admits as heavy: a gate suite sent as exclusive work held every slot of another Mac for 33
    minutes, queued and then running, while every other lane went to this Mac (2026-11-12). Any other command,
    an exclusive lane's, or a self lane's, whose script admits its own parts, is not refused."""
    words = list(command)
    for name, lane in (config.get("lanes") or {}).items():
        if not isinstance(lane, dict) or "parallel" in lane: continue
        if words not in (lane.get("command"), lane.get("compileFirst")): continue
        try:
            if admission(lane) != "heavy": continue
        except ValueError:
            continue
        return (f"the command is the {name} lane's, which its configuration admits as heavy work, never exclusive: "
                f"run it with `qk run {name}`, or, if its run "
                f"really needs the host to itself, give the lane \"resource\": \"exclusive\"")
    return None
# The platforms `qk app` builds an app for, and whether each is kept after every gated landing unless the
# project says otherwise: the Mac build is, so that the newest landed app opens at once; a device build is
# made when a device asks for it (app_builds.py).




# The lane the merge queue runs after the preflight lanes and before its gate where a project has one: the setup a gate
# assumes.
PROVISION = "provision"
# Why queuekeeper.json's spelling and queue.spelling are refused. Spelling is no concern of Queuekeeper's: a project
# that wants a spelling check, a linter or a formatter check makes it a lane of its own and lists it in
# queue.preflight (vocaro/queuekeeper#69).
SPELLING_GONE = ("is no longer a Queuekeeper setting, and Queuekeeper has no spelling check: make the project's check "
                 "a lane of its own, list it in queue.preflight, and keep its exemptions where that check reads them; "
                 "the user guide's Preflight lanes says how")
# A project's launcher: its copy of this repository's scripts/qk, which runs Queuekeeper on the project it is in.
LAUNCHER = "scripts/qk"
# The toolkit's docs check, the docs lane of a project that has none (implicit_command, lane_to_run).
DOCS = "docs"
IMPLICIT_DOCS = ["python3", LAUNCHER, "docs", "check"]
# What the toolkit's docs check asks of Queuekeeper, by whichever command runs it (implicit_command).
IMPLICIT_WORDS = {DOCS: ["docs", "check"]}
# Conventional prose only; configuration, agent policy, source and test fixtures keep their code gates. guides/ and
# knowledge/ are no standard documentation folders, so a project that keeps documents there names them in
# queue.unread.paths (vocaro/queuekeeper#106).
DOCS_FILES = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"}
DOCS_FOLDERS = ("doc/", "docs/")
# The older settings that queue.unread replaces: docsOnly, the paths a batch could change and skip the gate, and
# carryOver, the paths main could change under a passed result. Both said a path no code lane reads, in two lists that
# could disagree; a release accepts them, read as queue.unread (unread_declared), and the next refuses them.
UNREAD_LEGACY = ("docsOnly", "carryOver")
# Seconds a preflight lane may run before qk land queues an entry, and its timeout when its lane gives none
# (vocaro-engineering#283, vocaro/queuekeeper#69). A preflight lane is a cheap check, never a build.
PREFLIGHT_LIMIT = 300
# What a lane's command reads for the project's main checkout on the Mac that runs it, from which a launcher finds the
# tools beside it: a preflight lane runs in a scratch worktree, and a queue's lanes in its worker's checkout, neither of
# which has the project's neighbors beside it (vocaro/queuekeeper#69).
PROJECT_CHECKOUT_VARIABLE = "QUEUEKEEPER_PROJECT_CHECKOUT"
# The lane keys a preflight lane cannot have: it is one command, run before the queue takes an entry, in a scratch
# worktree on the Mac that ran qk land, with no admission.
NOT_PREFLIGHT = ("parallel", "foreground", "unlockedScreen", "wholeHost", "model", "compileFirst")


def conventional_doc(path):
    """Whether path is conventional prose, which queue.unread takes unless its defaults are false."""
    return path in DOCS_FILES or (path.startswith(DOCS_FOLDERS) and path.endswith((".md", ".markdown")))


def names_path(path, key):
    """Whether a queue setting's key names path: a directory ending in /, or the file itself."""
    return path.startswith(key) if key.endswith("/") else path == key


def unread_declared(queue):
    """queue.unread as the queue reads it: False, or an object of paths, exclude, lanes and defaults. A project still on
    the older queue.docsOnly and queue.carryOver has them read as one: carryOver's paths join docsOnly's, and an
    explicit docsOnly, which replaced the Markdown defaults, keeps them off. docsOnly false gives False, and beside a
    carryOver, which has no exact reading as queue.unread, its paths still carry over (legacy_carried) until the
    owner chooses one."""
    if "unread" in queue: return queue["unread"]
    docs, carried = queue.get("docsOnly"), queue.get("carryOver") or {}
    if docs is False: return False
    if docs is None: return {"paths": dict(carried)} if carried else {}
    return {"paths": {**docs["paths"], **carried}, "exclude": dict(docs.get("exclude", {})),
            "lanes": list(docs["lanes"]), "defaults": False}


def legacy_carried(config, path):
    """Whether path carries over only by the older queue.docsOnly false beside a queue.carryOver that names it: every
    batch gates, yet those paths still leave a passed result standing, as they did before queue.unread. One list
    cannot say both, so this reading lasts only as long as the older settings do (legacy_unread_warning)."""
    queue = config.get("queue", {})
    return "unread" not in queue and queue.get("docsOnly") is False \
        and any(names_path(path, key) for key in queue.get("carryOver") or {})


def unread(config, path):
    """Whether no code lane reads path, by main's queue.unread (unread_declared): a batch that changes only such paths
    runs the docs lanes in place of the gate, and main changing one the batch does not leaves a passed result standing.
    exclude wins over the Markdown defaults and paths."""
    declared = unread_declared(config.get("queue", {}))
    if declared is False: return False
    if any(names_path(path, key) for key in declared.get("exclude", {})): return False
    if declared.get("defaults", True) and conventional_doc(path): return True
    return any(names_path(path, key) for key in declared.get("paths", {}))


def unread_named(config, changed):
    """The lanes main's queue.unread.paths name for the paths changed, beyond queue.unread.lanes, each with the paths
    that name it and why: {lane: {path: why}}, in order. A path that a small check reads, and no other code lane, names
    that check's lanes, so that a batch of such paths runs them in place of the gate (vocaro/queuekeeper#27). An
    excluded path names none."""
    declared = unread_declared(config.get("queue", {}))
    if declared is False: return {}
    named = {}
    for path in changed:
        if any(names_path(path, key) for key in declared.get("exclude", {})): continue
        for key, value in declared.get("paths", {}).items():
            if isinstance(value, dict) and names_path(path, key):
                for lane in value.get("lanes", []): named.setdefault(lane, {})[key] = value["why"]
    return named


def unread_lanes(config):
    """The lanes a batch of unread paths runs in place of the gate, without the preflight lanes; None when main's
    queue.unread is false, so that every batch gates."""
    declared = unread_declared(config.get("queue", {}))
    return None if declared is False else list(declared.get("lanes", [DOCS]))


def gate_lanes(config):
    """The lanes main names as its gate, in order: queue.lanes, or gate where it gives none."""
    named = config.get("queue", {}).get("lanes")
    return list(named) if named else (["gate"] if "gate" in config.get("lanes", {}) else [])


def matches(path, pattern):
    """Whether pattern, a glob of queue.affected, matches the whole of path: * and ? stay within one directory and **
    spans any number of them."""
    return PurePosixPath(path).full_match(pattern)


def affected_by(config, path):
    """The gate lanes main's queue.affected selects for path, a changed path: those of every rule that matches it,
    none for a path queue.unread takes, which no gate lane reads, and None for a path no rule covers, which selects
    every gate lane (fail closed). None too where main gives no queue.affected."""
    rules = config.get("queue", {}).get("affected")
    if not rules: return None
    if unread(config, path): return set()
    found = [rule for rule in rules if any(matches(path, pattern) for pattern in rule["paths"])]
    return set().union(*(rule["lanes"] for rule in found)) if found else None


def affected_lanes(config, changed):
    """The gate lanes (gate_lanes) a batch changing the paths changed runs, in their order: those its paths select by
    main's queue.affected (affected_by), every one when a path no rule covers changed, when queue.affected is not
    given, or when no path selects any, as a batch of unread paths alone, which runs the docs lanes instead."""
    named = gate_lanes(config)
    selected = set()
    for path in changed:
        lanes = affected_by(config, path)
        if lanes is None: return named
        selected |= lanes
    return [name for name in named if name in selected] or named


def legacy_unread_warning(config):
    """qk doctor's and qk land's warning for a project that still gives queue.docsOnly or queue.carryOver, which a later
    release refuses, with the queue.unread that reads the same, or the choice to make where none does; else None."""
    queue = config.get("queue", {})
    given = [key for key in UNREAD_LEGACY if key in queue]
    if not given: return None
    declared = unread_declared(queue)
    if isinstance(declared, dict) and not declared: return None
    said = " and ".join(f"queue.{key}" for key in given)
    went = f"{said} {'go' if len(given) > 1 else 'goes'} in a later release"
    if declared is False and queue.get("carryOver"):
        paths = json.dumps(queue["carryOver"], sort_keys=True)
        return (f"{went}, and queue.unread, which replaces them, cannot say both that every batch gates and that "
                f"{', '.join(sorted(queue['carryOver']))} carr{'ies' if len(queue['carryOver']) == 1 else 'y'} over: "
                f"give \"queue\": {{\"unread\": false}} to carry nothing over, or {{\"unread\": {{\"paths\": {paths}, "
                "\"defaults\": false}} to let those paths also skip the gate")
    return (f"{went}: give \"queue\": {{\"unread\": {json.dumps(declared, sort_keys=True)}}} in place of {said}, "
            "which the queue already reads it as. Markdown outside doc/ and docs/ no longer "
            "carries over unless paths names it")


def preflight(config):
    """The project's preflight lanes, in order: the cheap checks the queue runs on an entry before it queues it, and
    first in every round, ungated landing and docs-only batch. queue.preflight names them; a project that gives none has
    none (vocaro/queuekeeper#69)."""
    return list(config.get("queue", {}).get("preflight", []))


def project_checkout(root):
    """The project's main checkout for a lane run at root: the main checkout of the repository root is a linked
    worktree of, else root, for PROJECT_CHECKOUT_VARIABLE."""
    from .git_state import main_checkout
    return str(Path(main_checkout(root) or root).resolve())


def landing_lanes(lanes, queue):
    """Each lane a landing runs, with why: the cheap checks before the gate, queue.lanes or gate, the lanes a
    docs-only batch runs instead, and every lane a group among them runs."""
    named = queue.get("lanes") or (["gate"] if "gate" in lanes else [])
    first = [name for name in preflight({"lanes": lanes, "queue": queue}) if name in lanes]
    found = {name: "the merge queue runs it before the gate" for name in [*first, PROVISION] if name in lanes}
    found.update({name: "the merge queue runs it as " + ("one of queue.lanes" if queue.get("lanes") else "the gate")
                  for name in named})
    found.update({name: "a docs-only batch runs it, by the default or queue.unread.lanes"
                  for name in unread_lanes({"queue": queue}) or [] if name not in found})
    declared = unread_declared(queue)
    for name, paths in unread_named({"queue": queue}, list(declared.get("paths", {}) if declared else ())).items():
        found.setdefault(name, f"a docs-only batch that changes {', '.join(paths)} runs it, by queue.unread.paths")
    waiting = list(found)
    while waiting:
        name = waiting.pop()
        lane = lanes.get(name)
        for member in lane.get("parallel", []) if isinstance(lane, dict) else []:
            if member not in found:
                found[member] = f"the lane group {name}, which a landing runs, runs it"
                waiting.append(member)
    return found


def release(declared, lanes=None, queue=None):
    """The release gate's settings: lanes, the release checks each a lane of its own, which eng release
    requires a pass of at the release commit, and maxCommits, the checkpoint's limit. A name in lanes that is no lane is qk doctor's warning, not an error.
    A release lane that a landing also runs is an error: a release check runs once, on the release
    commit, and no change waits for one to land."""
    if not isinstance(declared, dict) or set(declared) - {"lanes", "maxCommits"}:
        raise ValueError("release supports only lanes and maxCommits")
    named = declared.get("lanes", [])
    if not isinstance(named, list) or any(not isinstance(name, str) or not name.strip() for name in named) \
            or len(set(named)) != len(named) or "gate" in named:
        raise ValueError("release.lanes must list distinct lane names, never gate, which eng release checks first")
    limit = declared.get("maxCommits", 100)
    if type(limit) is not int or limit <= 0:
        raise ValueError("release.maxCommits must be a positive integer")
    landing = landing_lanes(lanes or {}, queue or {})
    for name in named:
        if name in landing:
            raise ValueError(f"release.lanes names {name}, which a landing runs too: {landing[name]}. A release "
                             "check runs once, on the release commit, so take it out of the landing lanes or out of "
                             "release.lanes")


def budget(name, lane):
    """budgetMinutes, the gate's time budget, from its measured time, past which qk land and qk doctor warn.
    Only the gate lane has one."""
    if not isinstance(lane, dict) or "budgetMinutes" not in lane: return
    if name != "gate":
        raise ValueError(f"{name}.budgetMinutes: only the gate lane has a time budget")
    if type(lane["budgetMinutes"]) is not int or lane["budgetMinutes"] <= 0:
        raise ValueError("gate.budgetMinutes must be a positive integer")


def merge_queue(queue, lanes):
    """The lanes a commit passes in the merge queue, what main may change under a passed result,
    each path with why no such lane reads it, the commands that main's change must pass first, and
    which of the lanes cover the pre-push hook, each with what of the hook it runs, the paths a
    docs-only batch changes, each with why no code lane reads it, with the lanes it runs instead, and how
    many runs in a row may give no verdict before the queue holds.
    unread, the paths no code lane reads, replaces docsOnly and carryOver, which it cannot stand beside."""
    if isinstance(queue, dict) and "spelling" in queue: raise ValueError(f"queue.spelling {SPELLING_GONE}")
    if not isinstance(queue, dict) or set(queue) - {"lanes", "carryOver", "carryOverChecks", "hookCoveredBy", "docsOnly",
                                                    "unread", "noVerdictRuns", "followLandings", "preflight",
                                                    "affected"}:
        raise ValueError("queue supports only lanes, unread, affected, carryOverChecks, hookCoveredBy, noVerdictRuns, "
                         "followLandings and preflight, and the older docsOnly and carryOver")
    if "unread" in queue:
        given = [key for key in UNREAD_LEGACY if key in queue]
        if given:
            raise ValueError(f"queue.unread and queue.{' and queue.'.join(given)} cannot both be given: queue.unread "
                             f"replaces {' and '.join(given)}, so move {'its' if len(given) == 1 else 'their'} paths "
                             "into queue.unread.paths")
        if queue["unread"] is not False and not unread_object(queue["unread"], lanes):
            raise ValueError("queue.unread is false to gate every batch and carry nothing over, or an object of "
                             "optional paths, mapping each path, a directory ending in /, to why no code lane "
                             "reads it, or to an object of that why and lanes, the distinct configured lanes, never "
                             "gate, of the few checks that do read it, which a batch changing it runs too; "
                             "exclude, the paths among them or the Markdown defaults that code does read, each with "
                             "why; lanes, the distinct configured lanes a batch of unread paths runs instead, never "
                             "gate; and defaults, false to drop the Markdown defaults")
    if "preflight" in queue: preflight_lanes(queue, lanes)
    if "affected" in queue: affected_rules(queue, lanes)
    if "followLandings" in queue and queue["followLandings"] is not True:
        raise ValueError("queue.followLandings can only be true, which has the queue fast-forward the project's main "
                         "checkout after each landing, for a checkout other projects read, such as an extension's")
    if "noVerdictRuns" in queue and (type(queue["noVerdictRuns"]) is not int or queue["noVerdictRuns"] < 1):
        raise ValueError("queue.noVerdictRuns is how many runs in a row may give no verdict before the queue holds, "
                         "a positive integer")
    named = queue.get("lanes", [])
    if not isinstance(named, list) or len(set(map(str, named))) != len(named) or any(name not in lanes for name in named):
        raise ValueError("queue.lanes must list distinct configured lanes")
    carried = queue.get("carryOver", {})
    if not isinstance(carried, dict) or any(not key.strip() or not isinstance(why, str) or not why.strip()
                                            for key, why in carried.items()):
        raise ValueError("queue.carryOver maps each path, a directory ending in /, to why no queue lane reads it")
    if "hookCoveredBy" in queue:
        covering = queue["hookCoveredBy"]
        passed = {*preflight({"lanes": lanes, "queue": queue}), PROVISION, *(named or ["gate"])} & set(lanes)
        if not isinstance(covering, dict) or not covering or any(lane not in passed or not isinstance(what, str)
                                                                 or not what.strip() for lane, what in covering.items()):
            raise ValueError("queue.hookCoveredBy maps each lane the queue runs that covers the pre-push hook to what "
                             "of the hook it runs")
    if "docsOnly" in queue and queue["docsOnly"] is not False and not docs_only(queue["docsOnly"], lanes):
        raise ValueError("queue.docsOnly is false to disable docs-only landings, or holds paths, mapping each path, "
                         "a directory ending in /, to why no code lane "
                         "reads it, and lanes, the distinct configured lanes a docs-only batch runs instead, never gate, "
                         "whose receipt eng release reads; optional exclude maps code-read paths to their reasons")
    checks = queue.get("carryOverChecks", [])
    if not isinstance(checks, list) or any(not carry_over_check(check) for check in checks):
        raise ValueError("queue.carryOverChecks lists objects with a command, an argv list and never shell source, and "
                         "optional paths, a nonempty list limiting it to moves of those paths, a directory ending in /")


def preflight_lanes(queue, lanes):
    """queue.preflight, checked: distinct configured lanes, in the order they run, each one command that qk land can run
    in a scratch worktree with no admission (NOT_PREFLIGHT), and none of them the gate, provision or one of
    queue.lanes (vocaro/queuekeeper#69)."""
    named = queue["preflight"]
    if not isinstance(named, list) or any(not isinstance(name, str) or not name.strip() for name in named) \
            or len(set(named)) != len(named):
        raise ValueError("queue.preflight lists distinct lane names, the cheap checks the queue runs on an entry before "
                         "it queues it and first in every round, in order; [] for none")
    for name in named:
        if name not in lanes: raise ValueError(f"queue.preflight names {name}, which is no configured lane")
        if name in ("gate", PROVISION) or name in queue.get("lanes", []):
            raise ValueError(f"queue.preflight names {name}, which the queue runs after the preflight lanes")
        keys = [key for key in (*NOT_PREFLIGHT, *hooks.LANE_KEYS) if lanes[name].get(key)]
        if keys or unadmitted(lanes[name]):
            raise ValueError(f"queue.preflight names {name}, which is not one command that qk land can run before it "
                             f"queues an entry, unadmitted, in a scratch worktree: "
                             f"{'it has ' + ', '.join(keys) if keys else 'it is a self lane'}")


def affected_rules(queue, lanes):
    """queue.affected, checked: a nonempty list of rules, each of paths, distinct globs, and lanes, distinct gate lanes
    (gate_lanes) that read the paths those globs match."""
    rules, named = queue["affected"], gate_lanes({"lanes": lanes, "queue": queue})
    def distinct(items): return isinstance(items, list) and items and len(set(map(str, items))) == len(items) \
        and all(isinstance(item, str) and item.strip() for item in items)
    if not isinstance(rules, list) or not rules or any(not isinstance(rule, dict) or set(rule) != {"paths", "lanes"}
                                                       or not distinct(rule["paths"]) or not distinct(rule["lanes"])
                                                       for rule in rules):
        raise ValueError("queue.affected lists rules, each an object of paths, distinct globs of the paths a few gate "
                         "lanes read (* within one directory, ** across them), and lanes, those gate lanes: a batch "
                         "runs the gate lanes its changed paths select, and every one for a path no rule covers")
    for rule in rules:
        for name in rule["lanes"]:
            if name not in named:
                raise ValueError(f"queue.affected names {name}, which is not one of the gate lanes "
                                 f"({', '.join(named) or 'none'}), queue.lanes or else gate")
        for pattern in rule["paths"]:
            if pattern.startswith("/") or not PurePosixPath(pattern).parts:
                raise ValueError(f"queue.affected's glob {pattern} is no path relative to the repository")


def unread_object(declared, lanes):
    """Whether declared is a valid queue.unread object (merge_queue)."""
    if not isinstance(declared, dict) or set(declared) - {"paths", "exclude", "lanes", "defaults"}: return False
    def said(why): return isinstance(why, str) and why.strip()
    def runs(named): return isinstance(named, list) and named and len(set(map(str, named))) == len(named) \
        and all(isinstance(name, str) and (name in lanes or name == DOCS) and name != "gate" for name in named)
    def reasons(value, laned=False): return isinstance(value, dict) and all(
        isinstance(key, str) and key.strip() and (said(why) or laned and isinstance(why, dict)
                                                  and set(why) == {"why", "lanes"} and said(why["why"])
                                                  and runs(why["lanes"])) for key, why in value.items())
    if not reasons(declared.get("paths", {}), laned=True) or not reasons(declared.get("exclude", {})): return False
    if "defaults" in declared and type(declared["defaults"]) is not bool: return False
    if declared.get("defaults") is False and not declared.get("paths"): return False
    return runs(declared.get("lanes", [DOCS]))


def docs_only(declared, lanes):
    if not isinstance(declared, dict) or not {"paths", "lanes"} <= set(declared) \
            or set(declared) - {"paths", "lanes", "exclude"}: return False
    excluded = declared.get("exclude", {})
    if not isinstance(excluded, dict) or any(not isinstance(key, str) or not key.strip() or not isinstance(why, str)
                                            or not why.strip() for key, why in excluded.items()): return False
    paths, named = declared["paths"], declared["lanes"]
    return isinstance(paths, dict) and paths and all(isinstance(key, str) and key.strip() and isinstance(why, str)
                                                      and why.strip() for key, why in paths.items()) \
        and isinstance(named, list) and named and len(set(map(str, named))) == len(named) \
        and all((name in lanes or name == DOCS) and name != "gate" for name in named)


def carry_over_check(check):
    def words(value): return isinstance(value, list) and value and all(isinstance(x, str) and x.strip() for x in value)
    return isinstance(check, dict) and not set(check) - {"command", "paths"} and words(check.get("command")) \
        and ("paths" not in check or words(check["paths"]))


def retired(lanes):
    """Refuse "buildHost", true or false, on any lane or group. It once sent a lane to another Mac, and
    vocaro-engineering#58 removed it: every heavy or exclusive lane goes there unless it says why it stays,
    so a checkout that still has it is older than its project's change, and reading it as nothing could
    move that checkout's work without a word."""
    marked = [name for name, lane in lanes.items() if isinstance(lane, dict) and "buildHost" in lane]
    if not marked: return
    named = ", ".join(marked[:-1]) + " and " + marked[-1] if len(marked) > 1 else marked[0]
    raise ValueError(f"queuekeeper.json gives {named} \"buildHost\", which vocaro-engineering#58 removed: every heavy "
                     "or exclusive lane goes to another Mac unless it says why it stays, with \"here\": \"REASON\", "
                     f"so delete {'it' if len(marked) == 1 else 'each'}, and rebase a checkout older than that change "
                     "onto main")


def place(name, lane):
    """Every admitted lane is placed among the Macs unless "here" says why it stays on this Mac, such as
    a physical device or a measurement tied to this Mac's hardware, like goldens rendered on its GPU."""
    if "here" not in lane: return
    if not isinstance(lane["here"], str) or not lane["here"].strip():
        raise ValueError(f"{name}.here says why the lane stays on this Mac, such as a physical device or a "
                         "measurement tied to this Mac's hardware: a nonempty reason")
    if unadmitted(lane):
        raise ValueError(f"{name}.here keeps a lane on this Mac, and a self lane runs where it starts already")


# A label, as a lane names it and a Mac's settings give it (offload_host.LABEL): lowercase words joined by hyphens.
LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def labels(name, lane):
    """A lane's "labels" are what it needs of the Mac that runs it, such as a license or a piece of hardware: it runs
    only on a Mac whose own settings list every one (offload_host.mac_settings; the owner's decision, 2026-11-17,
    vocaro/queuekeeper#211). A Mac's settings name no lane, and a lane names no Mac. A self lane is not placed."""
    if "labels" not in lane: return
    wanted = lane["labels"]
    if not isinstance(wanted, list) or not wanted or any(not isinstance(label, str) or not LABEL.fullmatch(label)
                                                         for label in wanted) or len(set(wanted)) != len(wanted):
        raise ValueError(f"{name}.labels must be a non-empty list of labels a Mac's settings give it, each lowercase "
                         "words joined by hyphens and named once, such as [\"unity-license\"]")
    if unadmitted(lane):
        raise ValueError(f"{name}.labels needs an admitted lane: queuekeeper places only an admitted lane on a Mac, "
                         "and a self lane runs where it starts")


def needed_labels(lanes, name):
    """The labels the Mac that runs the lane name must have: its own and, for a group, which runs its lanes on one
    Mac, those of each of its lanes, each once, in the order first named."""
    lane = lanes.get(name, {})
    members = [lanes.get(member, {}) for member in lane.get("parallel", [])]
    return tuple(dict.fromkeys(label for each in [lane, *members] for label in each.get("labels", [])))


def whole_host_lane(lanes, name):
    """Whether the lane name takes every guaranteed slot of the Mac it runs on: a lane, or a group, that says
    "wholeHost": true. A group that runs such a lane says so itself, as validation requires."""
    return bool(lanes[name].get("wholeHost"))


def apps(name, lane):
    """A foreground lane's "apps" names the app under test, as a crash-report alert names the process that quit,
    such as "EPUBLib": a failed run during which queuekeeper cleared an alert for any other process, one that stood longer
    than one look, is unmeasured, while one for these stays failed (crash_alerts.py, vocaro-engineering#226)."""
    if "apps" not in lane: return
    names = lane["apps"]
    if not isinstance(names, list) or not names or any(not isinstance(app, str) or not app.strip() or app != app.strip()
                                                       for app in names):
        raise ValueError(f"{name}.apps names the app under test, as a crash-report alert names its process: a list "
                         "of nonempty names")
    if not lane.get("foreground"):
        raise ValueError(f"{name}.apps needs a foreground lane: queuekeeper clears crash-report alerts only during one")


def screen(name, lane):
    """A lane that needs an unlocked console screen, "unlockedScreen": true, such as a Mac suite whose tests read
    their own window through the accessibility API, which a lock hides, runs only on a Mac whose screen queuekeeper reads
    as unlocked when it places the lane, and otherwise waits for one, instead of running unmeasured
    (vocaro-engineering#185). A self lane is not placed."""
    if lane.get("unlockedScreen") and unadmitted(lane):
        raise ValueError(f"{name}.unlockedScreen needs an admitted lane: queuekeeper places only an admitted lane on a Mac, "
                         "and a self lane runs where it starts")


def unlocked_screen(lanes, name):
    """Whether the lane name needs an unlocked console screen on the Mac it runs on: a lane, or a group, that says
    "unlockedScreen": true. A group that runs such a lane says so itself, as validation requires."""
    return bool(lanes.get(name, {}).get("unlockedScreen"))


def toolkit_command(root, words, toolkit=None):
    """The command that runs Queuekeeper's words on the project at root: its launcher, `python3 scripts/qk`, when it
    has one, which finds the toolkit itself; otherwise the toolkit's own bin/qk, with the Python that runs this
    Queuekeeper, and the project named. The toolkit is toolkit, else the one QUEUEKEEPER_ROOT names, as a launcher
    reads it, else this one. A project need not hold a launcher for what the queue runs in it: on 2026-11-13
    apple-engineering's first landing failed in the implicit spelling lane, as python3 could not open the
    scripts/qk that getting started never asked it for (vocaro/queuekeeper#108)."""
    root = Path(root)
    if (root / LAUNCHER).is_file(): return ["python3", LAUNCHER, *words]
    named = toolkit or os.environ.get("QUEUEKEEPER_ROOT")
    kit = (root / named).resolve() if named else TOOLKIT_ROOT
    return [sys.executable, str(kit / "bin/qk"), "--project", str(root.resolve()), *words]


def implicit_command(root, name, toolkit=None):
    """The command of the toolkit's docs check, name in IMPLICIT_WORDS, in the project at root: its launcher's
    IMPLICIT_DOCS, else the same words run by the toolkit (toolkit_command). qk run makes it on the Mac that runs the
    lane, so that on another Mac it names the toolkit sent there."""
    return toolkit_command(root, IMPLICIT_WORDS[name], toolkit)


def lane_to_run(config, name, root):
    """The lane qk run runs as name in the project at root, or None when there is no such lane: the one config has,
    or, for docs in a project with no such lane, the toolkit's check (implicit_command), added to config's lanes so
    that the run treats it as any other, which is how the merge queue's gate runs it (owner's decision, 2026-11-09,
    vocaro-engineering#305)."""
    lanes = config.setdefault("lanes", {})
    if name in IMPLICIT_WORDS and name not in lanes:
        lanes[name] = {"command": implicit_command(root, name), "timeout": PREFLIGHT_LIMIT}
    # A lane queue.preflight lists that gives no timeout is a cheap check, stopped at PREFLIGHT_LIMIT, not
    # processes.DEFAULT_LIMIT.
    if name in config.get("queue", {}).get("preflight", []) and isinstance(lanes.get(name), dict) \
            and "command" in lanes[name]:
        lanes[name].setdefault("timeout", PREFLIGHT_LIMIT)
    return lanes.get(name)


def stays(lanes, name):
    """Why the lane name runs only on this Mac, as its configuration says, or None for a lane that goes to another
    Mac first: a self lane runs where it starts, and a lane, or a lane of a group, says why with "here". A
    group runs its lanes under its one admission on one Mac, so a lane of it that stays here keeps the group
    here. A light lane is placed as every lane is (the owner's decision, 2026-11-07). A foreground lane goes to another
    Mac too, and only there (foreground.py)."""
    lane = lanes[name]
    if unadmitted(lane): return "a self lane runs where it starts, and its script admits each of its parts"
    if lane.get("here"): return f"the lane says: {lane['here']}"
    for member in lane.get("parallel", []):
        if lanes[member].get("here"): return f"its lane {member} says: {lanes[member]['here']}"
    # A key an extension added may keep the lane here too, such as the chips its results are valid on.
    placed = hooks.placement(lanes, name)
    return placed[1] if placed and placed[0] == {hooks.HERE} else None


def model(name, lane):
    """A generative-model lane takes one heavy slot and the host's model lock. A self lane is not
    admitted, and exclusive admission holds the model already."""
    if lane.get("model") and admission(lane) != "heavy":
        raise ValueError(f"{name}.model needs a heavy lane: a self lane is not admitted, and exclusive "
                         "admission holds the model already")


def whole_host(name, lane):
    """A whole-host lane, "wholeHost": true, is a Mac's work: a gate that runs the project's suites side by
    side. It takes every guaranteed slot of the Mac it runs on, so that a second such lane never runs
    beside it there, and queuekeeper sends it to the Mac that has them all free, while a lighter heavy lane still
    shares (vocaro-engineering#168). A self lane is not
    admitted, and exclusive admission holds the host already."""
    if lane.get("wholeHost") and admission(lane) != "heavy":
        raise ValueError(f"{name}.wholeHost needs a heavy lane: a self lane is not admitted, and exclusive "
                         "admission holds the host already")


def group(name, lane, lanes):
    """A lane group names other lanes to run side by side under its one admission, so it needs
    the heaviest resource among them, the model when one of them runs a model, and one lane at a
    time when several do."""
    if set(lane) - GROUP_KEYS:
        raise ValueError(f"Lane group {name} takes only parallel, jobs, resource, here, model, wholeHost, "
                         "unlockedScreen, labels, failFast and budgetMinutes")
    members = lane["parallel"]
    if not isinstance(members, list) or not members or len(set(members)) != len(members) \
            or any(not isinstance(member, str) for member in members):
        raise ValueError(f"{name}.parallel must list distinct lane names")
    resource = resource_of(name, lane)
    if resource == "self":
        raise ValueError(f"{name} runs its lanes under its one admission, so it cannot be a self lane, which takes none")
    for member in members:
        other = lanes.get(member)
        if not isinstance(other, dict) or "command" not in other:
            raise ValueError(f"{name}.parallel names {member}, which is not a lane with a command")
        if other.get("foreground"):
            raise ValueError(f"{name}.parallel names {member}, which takes the screen and runs alone")
        kind = resource_of(member, other)
        if kind == "self":
            raise ValueError(f"{name} runs {member} under its own admission, inside which {member}'s script could not "
                             "take the admissions of its own that a self lane takes")
        if WEIGHT[kind] > WEIGHT[resource]:
            raise ValueError(f"{name} runs {member} under its own admission, so it needs a resource of at least {kind}")
        if other.get("model") and not lane.get("model") and resource != "exclusive":
            raise ValueError(f"{name} runs {member} under its own admission, so it needs \"model\": true or an "
                             "exclusive resource")
        if other.get("wholeHost") and not lane.get("wholeHost") and resource != "exclusive":
            raise ValueError(f"{name} runs {member} under its own admission, so it needs \"wholeHost\": true or an "
                             "exclusive resource")
        if other.get("unlockedScreen") and not lane.get("unlockedScreen"):
            raise ValueError(f"{name} runs {member}, which needs an unlocked screen, on the Mac it is placed on, so it "
                             "needs \"unlockedScreen\": true too")
    if "jobs" in lane and (type(lane["jobs"]) is not int or not 1 <= lane["jobs"] <= len(members)):
        raise ValueError(f"{name}.jobs must be from 1 to the number of its lanes")
    # One model workload per machine: the group's one model lock cannot keep its own lanes apart.
    models = [member for member in members if lanes[member].get("model")]
    if len(models) > 1 and lane.get("jobs", len(members)) != 1:
        raise ValueError(f"{name} runs the model lanes {', '.join(models)}, which would drive the model at once, "
                         "so it needs \"jobs\": 1")
    for key in ["model", "wholeHost", "unlockedScreen", "failFast"]:
        if key in lane and type(lane[key]) is not bool:
            raise ValueError(f"{name}.{key} must be true or false")
    place(name, lane)
    labels(name, lane)
    model(name, lane)
    whole_host(name, lane)
    screen(name, lane)


def source_identity(root):
    import subprocess
    import hashlib
    root = Path(root).resolve()
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=30)
    revision = result.stdout.strip() if result.returncode == 0 else None
    diff = subprocess.run(["git", "-C", str(root), "diff", "--binary", "HEAD"], capture_output=True, timeout=30)
    untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
                               capture_output=True, timeout=30)
    digest = hashlib.sha256(diff.stdout)
    for raw in sorted(untracked.stdout.split(b"\0")):
        if not raw: continue
        path = root / os.fsdecode(raw)
        digest.update(raw + b"\0")
        if path.is_symlink(): digest.update(os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return {"revision": revision, "workingChangesSHA256": digest.hexdigest()}
