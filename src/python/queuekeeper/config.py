"""Small, strict project configuration without third-party dependencies."""
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

from . import plugins as hooks, lane_timing, pins, sandbox

TOOLKIT_ROOT = Path(__file__).resolve().parents[3]
# The plugins a project gets when its queuekeeper.json names none, and that a command with no project loads.
PLUGINS = ()
FILE = "queuekeeper.json"
def plugins(declared):
    """A project's plugins, checked: each a module's dotted name, or {"module": NAME, "path": FOLDER} for one
    whose package is in FOLDER, a path relative to the project's main checkout."""
    if not isinstance(declared, list): raise ValueError("plugins must be a list")
    normalized = []
    for entry in declared:
        named = entry.get("module") if isinstance(entry, dict) else entry
        if not isinstance(named, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", named) or (
                isinstance(entry, dict) and (set(entry) - {"module", "path"} or not isinstance(entry.get("path", ""), str))):
            raise ValueError('plugins lists module names, or {"module": NAME, "path": FOLDER}')
        if named in (CHIPS_PLUGIN, __package__ + ".mac_hardware"): raise ValueError(f"plugins names {named}, {CHIPS_GONE}")
        unknown(named)
        normalized.append(dict(entry) if isinstance(entry, dict) else named)
    return normalized


def unknown(named):
    """Refuse a plugin named as a module of Queuekeeper's own package that it does not have, such as
    queuekeeper.apple: the plugins are packages of their own, named with an underscore (vocaro/queuekeeper#187).
    A folder with no module in it, such as one a checkout keeps from before the plugins left the core with only
    ignored files in it, imports as a namespace package, which has no origin, and is no module either."""
    if not named.startswith(__package__ + "."): return
    try: found = getattr(importlib.util.find_spec(named), "origin", None) is not None
    except ModuleNotFoundError: found = False
    if found: return
    plugin = __package__ + "_" + named.removeprefix(__package__ + ".")
    raise ValueError(f"plugins names {named}, which is no module of Queuekeeper's"
                     + (f"; the plugin is the package {plugin}: list {plugin}" if "." not in plugin and
                        importlib.util.find_spec(plugin) is not None else ""))


def plugins_of(root, base=None):
    """What the project at root switches on, as (module, folder or None) pairs: its queuekeeper.json's plugins,
    or PLUGINS when it names none or there is no readable project. A folder is resolved against base, the
    project's main checkout when root is a linked worktree of it, else against root; but a folder beside the project
    that the run this process belongs to pinned (pins.held) is that run's checkout of it, so that every command of
    a run loads what the run started with (vocaro-engineering#497). A pin set that is not what it says is
    pins.Unpinned."""
    try: declared = plugins(json.loads((Path(root) / FILE).read_text()).get("plugins", list(PLUGINS)))
    except (OSError, ValueError, AttributeError): declared = list(PLUGINS)
    pinned = pins.held()
    pins.verify({name: pinned[name] for name in sibling_folders(root) if name in pinned} if pinned else None)
    return [(entry["module"], (pins.folder(entry["path"], pinned) or str((Path(base or root) / entry["path"]).resolve()))
             if entry.get("path") else None)
            if isinstance(entry, dict) else (entry, None) for entry in declared]


# The key's name before #48. A project that still lists its plugins under it loads none of them.
EXTENSIONS_RENAMED = ("extensions is now plugins (vocaro/queuekeeper#48): rename the key in queuekeeper.json, which "
                      "lists the same modules")


def renamed_plugins_key(root):
    """What to say when the project at root still lists its plugins under "extensions", or None: plugins_of loads
    none of them, so a command one adds is missing, and argparse says only that no such command exists (#371)."""
    try: value = json.loads((Path(root) / FILE).read_text())
    except (OSError, ValueError): return None
    if not isinstance(value, dict) or "extensions" not in value: return None
    listed = value["extensions"] if isinstance(value["extensions"], list) else []
    names = [entry.get("module") if isinstance(entry, dict) else entry for entry in listed]
    names = ", ".join(name for name in names if isinstance(name, str)) or "none"
    return (f"{EXTENSIONS_RENAMED}. Until then no plugin it names is loaded ({names}), so the commands they add "
            f"are missing")


def sibling_folders(root):
    """The folders beside the project at root that a run of it loads from, by name (sibling_variables)."""
    return list(sibling_variables(root))


def sibling_variables(root):
    """The sibling folders a project loads from, {NAME: VARIABLE or None}."""
    return {name: sibling_variable(entry) for name, entry in sibling_declarations(root).items()}


def project_siblings(root):
    """The siblings named directly by the working tree at root."""
    try: return siblings_in(json.loads((Path(root) / FILE).read_text()))
    except FileNotFoundError: return {}


def sibling_declarations(root):
    """The project's siblings and those they declare in turn. For inventory, read the run's pinned
    checkouts where it has them, otherwise the shared checkouts. pins.make reads dependencies from
    the checkout it actually selects."""
    try: found = project_siblings(root)
    except (OSError, ValueError, AttributeError): return {}
    held = pins.held() or {}
    waiting = list(found)
    while waiting:
        name = waiting.pop(0)
        folder = held[name]["root"] if name in held else pins.clone(root, name)
        try: further = further_siblings(folder)
        except ValueError: continue
        for more, entry in further.items():
            if more not in found: waiting.append(more)
            if not found.get(more): found[more] = entry
    return found


def sibling_variable(entry):
    return entry["variable"] if isinstance(entry, dict) else entry


def sibling_revisions(entries):
    """Only the exact declarations, including the variable each one exports."""
    return {name: entry for name, entry in entries.items() if isinstance(entry, dict)}


def siblings_in(configuration):
    """The folders beside a project that its configuration declares, with a variable name, an exact
    declaration, or None: NAME for each plugin folder "../NAME/...", and each NAME of "siblings", a folder
    the project's own code loads from, found by the environment variable VARIABLE, which a run that pins the folder
    sets to its checkout of it (pins.exported). A ValueError says what is wrong with either."""
    found = {}
    for entry in plugins(configuration.get("plugins", [])):
        parts = Path(entry.get("path") or "").parts if isinstance(entry, dict) else ()
        if len(parts) > 1 and parts[0] == ".." and parts[1] not in ("..", "."): found.setdefault(parts[1], None)
    found.update(siblings(configuration.get("siblings", {})))
    return found


def siblings(declared):
    """Check sibling names, environment variables, full commit IDs and links. An object's "link" is left out of
    what this returns, so that an object with no "revision" is its variable alone, a sibling that follows its
    origin/main, as everything that selects commits reads it; sibling_links reads the links."""
    if not isinstance(declared, dict):
        raise ValueError("siblings must be an object")
    checked = {}
    for name, entry in declared.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
            raise ValueError(f"siblings names {name!r}, which is not a folder name")
        if isinstance(entry, dict):
            if not entry.keys() & {"revision", "link"} or entry.keys() - {"variable", "revision", "link"} \
                    or "variable" not in entry:
                raise ValueError(f'siblings.{name} needs "variable" and "revision", a full 40-digit Git commit ID, '
                                 f'or "link", a folder beside the checkout such as "../{name}"')
            if "revision" in entry and (not isinstance(entry["revision"], str)
                                        or not re.fullmatch(r"[0-9a-fA-F]{40}", entry["revision"])):
                raise ValueError(f'siblings.{name} needs "variable" and "revision", a full 40-digit Git commit ID')
            if "link" in entry: sibling_link(name, entry["link"])
        variable = sibling_variable(entry)
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable):
            raise ValueError(f"siblings.{name} needs an uppercase environment variable name")
        if not isinstance(entry, dict): checked[name] = entry
        elif "revision" in entry: checked[name] = {"variable": variable, "revision": entry["revision"].lower()}
        else: checked[name] = variable
    return checked


def sibling_link(name, link):
    """The folder a sibling's "link" names beside the checkout, "../FOLDER", checked: the one place a lane's checkout
    finds the sibling by a relative path, which Queuekeeper makes a link to the run's pinned checkout of it
    (pins.place_links). Nothing further out: the folder that holds a checkout holds only its own neighbours, such as
    the merge queue worker's queuekeeper-workers/NAME/, and not those of another project's."""
    found = re.fullmatch(r"\.\./([A-Za-z0-9_][A-Za-z0-9_.-]*)/?", link) if isinstance(link, str) else None
    if found is None:
        raise ValueError(f'siblings.{name}.link is {link!r}: name one folder beside the checkout, as "../{name}", '
                         "not an absolute path or one further out")
    return found[1]


def sibling_links(configuration):
    """The project's own siblings that name a "link", {NAME: FOLDER}, FOLDER beside its checkout. A sibling's own
    queuekeeper.json links nothing: a link is the project's, beside the checkout its lanes run in."""
    declared = configuration.get("siblings", {}) if isinstance(configuration, dict) else {}
    siblings(declared)
    return {name: sibling_link(name, entry["link"]) for name, entry in declared.items()
            if isinstance(entry, dict) and "link" in entry}


def further_siblings(folder):
    """What a sibling's checkout declares under "siblings" in its own queuekeeper.json."""
    try: declared = json.loads((Path(folder) / FILE).read_text()).get("siblings", {})
    except (OSError, ValueError, AttributeError): return {}
    return siblings(declared)


def load(root):
    return parse((Path(root).resolve() / FILE).read_text())


# The keys a lane and a lane group take, which parse checks and schemas/queuekeeper.schema.json lists (a test
# compares them, vocaro-engineering#248).
LANE_KEYS = frozenset({"command", "resource", "minimumTests", "timeout", "foreground", "here", "model", "display",
                       "budgetMinutes", "wholeHost", "apps", "unlockedScreen", "devices", "compileFirst",
                       "deviceRunMinutes", "stall", "unity", "arguments", "idioms", "dimensions", "exactTests",
                       "failedTests", "labels", "sandbox", "timing", "targetMinutes", "notice", "disk", "inputs",
                       "environmentInputs",
                       "testAllowance", "unmovedMinutes"})
# The lane keys above that a plugin shipped here adds (hooks.LANE_KEYS), by its module: a project that uses one
# without the plugin is told which to switch on.
PLUGIN_LANE_KEYS = {"display": "queuekeeper_mac_desktop", "unity": "queuekeeper_unity", "devices": "queuekeeper_mobile",
                       "deviceRunMinutes": "queuekeeper_mobile", "idioms": "queuekeeper_apple",
                       "dimensions": "queuekeeper_apple"}
# The top-level keys a plugin shipped here adds (hooks.CONFIG_KEYS), by its module, refused the same way.
PLUGIN_CONFIG_KEYS = {"unity": "queuekeeper_unity", "app": "queuekeeper_apple", "simulatorPool": "queuekeeper_apple",
                         "android": "queuekeeper_android", "emulatorPool": "queuekeeper_android",
                         "emulatorRetention": "queuekeeper_android"}
GROUP_KEYS = frozenset({"parallel", "jobs", "resource", "here", "model", "budgetMinutes", "wholeHost", "unlockedScreen",
                        "labels", "failFast", "disk", "inputs"})




def lane_devices(lane):
    """Reservations interpreted by the active device provider."""
    return hooks.DEVICES.lane_devices(lane)

# How a project's changes reach main (user guide, Pull requests or the merge queue): the merge queue pushing
# entries landed with qk land; pull requests a person merges, with no queue; or the merge queue landing pull
# requests a person labeled, as merge commits (vocaro/queuekeeper#376).
DIRECT_MAIN, PULL_REQUEST, PULL_REQUEST_QUEUE = "direct-main", "pull-request", "pull-request-queue"
INTEGRATIONS = (DIRECT_MAIN, PULL_REQUEST, PULL_REQUEST_QUEUE)
# The label by which a person admits a pull request to the queue, unless pullRequests.label names another.
QUEUE_LABEL = "queue"


def pull_request_settings(settings):
    if not isinstance(settings, dict): raise ValueError("pullRequests must be an object")
    unknown = set(settings) - {"label", "lanes"}
    if unknown: raise ValueError(f"Unknown pullRequests keys: {sorted(unknown)}")
    if "label" in settings and (not isinstance(settings["label"], str) or not settings["label"].strip()):
        raise ValueError("pullRequests.label must be a nonempty string")
    lanes = settings.get("lanes", ["gate"])
    if not isinstance(lanes, list) or not lanes or not all(isinstance(lane, str) and lane for lane in lanes):
        raise ValueError("pullRequests.lanes must be a nonempty list of lane names")


def check_lanes(config):
    """The lanes qk pr check runs on a pull request by itself, in a pull-request project (vocaro/queuekeeper#396):
    pullRequests.lanes, else gate."""
    return list(config.get("pullRequests", {}).get("lanes", ["gate"]))


def queue_label(config):
    """The label that admits a pull request to the queue of a pull-request-queue project."""
    return config.get("pullRequests", {}).get("label", QUEUE_LABEL)


def parse(source):
    """A project's queuekeeper.json, checked, from its text, wherever that came from."""
    value = json.loads(source)
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("queuekeeper.json requires schemaVersion 1")
    allowed = {"schemaVersion", "name", "plugins", "lanes", "integration", "pullRequests", "repository",
               "queue", "release", "testLanes", "gitHooks", "docs", "siblings", "disk", "queuekeeper"}
    if "spelling" in value: raise ValueError(f"spelling {SPELLING_GONE}")
    if "extensions" in value: raise ValueError(EXTENSIONS_RENAMED)
    if set(value) - allowed - set(PLUGIN_CONFIG_KEYS):
        raise ValueError(f"Unknown configuration keys: {sorted(set(value) - allowed - set(PLUGIN_CONFIG_KEYS))}")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("Project name must be a nonempty string")
    declared = plugins(value.get("plugins", list(PLUGINS)))
    if "plugins" in value: value["plugins"] = declared
    siblings(value.get("siblings", {}))
    # The plugins shipped here that the project switches on add keys to its lanes, so they are loaded before the
    # lanes are checked, whichever command reads the configuration. One in a folder of the project's is the
    # command's to load (cli.perform), which knows the folder.
    hooks.load([named for named in declared if isinstance(named, str) and (named.startswith(__package__ + ".") or named in set(PLUGIN_CONFIG_KEYS.values()) | set(PLUGIN_LANE_KEYS.values()))])
    for key, module in PLUGIN_CONFIG_KEYS.items():
        if key not in value: continue
        if key not in hooks.CONFIG_KEYS:
            raise ValueError(f"{key} is the {module} plugin's key, and the project does not switch that plugin "
                             f"on: list {module} in plugins")
        hooks.CONFIG_KEYS[key](value[key])
    git_hooks(value.get("gitHooks"))
    disk_settings("disk", value.get("disk", {}))
    if value.get("integration", DIRECT_MAIN) not in INTEGRATIONS:
        raise ValueError("Unknown integration policy")
    pull_request_settings(value.get("pullRequests", {}))
    if not isinstance(value.get("lanes", {}), dict):
        raise ValueError("lanes must be an object")
    lanes = value.get("lanes", {})
    retired(lanes)
    for name, lane in lanes.items():
        budget(name, lane)
        if isinstance(lane, dict) and "parallel" in lane:
            group(name, lane, lanes)
            continue
        if isinstance(lane, dict) and "chips" in lane: raise ValueError(f"{name}.chips, {CHIPS_GONE}")
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
        disk_settings(f"{name}.disk", lane.get("disk", {}))
        model(name, lane)
        whole_host(name, lane)
        screen(name, lane)
        compile_first(name, lane)
        stall(name, lane)
        unmoved_minutes(name, lane)
        lane_timing.check(name, lane)
        failed_tests_setting(name, lane)
        sandbox.check(name, lane)
        plugin_keys(name, lane)
        command = lane.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f"Lane {name} needs an argv list, never shell source")
        if "environmentInputs" in lane:
            names = lane["environmentInputs"]
            if not isinstance(names, list) or not names or any(
                    not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable)
                    for variable in names) or len(names) != len(set(names)):
                raise ValueError(f"{name}.environmentInputs must list distinct shell variable names")
        for key in ["minimumTests", "timeout", "testAllowance"]:
            if key in lane and (type(lane[key]) is not int or lane[key] <= 0):
                raise ValueError(f"{name}.{key} must be a positive integer")
        if lane.get("exactTests") and "minimumTests" not in lane:
            raise ValueError(f"{name}.exactTests needs the lane's minimumTests, the count it makes exact")
    merge_queue(value.get("queue", {}), lanes)
    lane_inputs_checked(value.get("queue", {}), lanes)
    release(value.get("release", {}), lanes, value.get("queue", {}))
    test_lanes(value.get("testLanes", {}))
    documentation(value.get("docs", {}))
    queuekeeper_settings(value.get("queuekeeper", {}))
    return value


# A Queuekeeper version, as a release's tag names it without its v (tools/release.py).
RELEASE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def queuekeeper_settings(declared):
    """queuekeeper, what the project needs of the Queuekeeper that runs it: minimum, the oldest release, such as
    "1.2.0", whose behavior it relies on (vocaro/queuekeeper#351). It is a floor, not a pin: every project on a Mac
    runs the one Queuekeeper checkout there, so a project cannot choose its own."""
    if not isinstance(declared, dict) or set(declared) - {"minimum"}:
        raise ValueError("queuekeeper supports only minimum")
    minimum = declared.get("minimum", "0.0.0")
    if not (isinstance(minimum, str) and RELEASE_VERSION.fullmatch(minimum)):
        raise ValueError("queuekeeper.minimum must be a release's version of the form X.Y.Z, such as 1.2.0")


def release_shortfall(config, running, toolkit=TOOLKIT_ROOT):
    """What to say when the project's queuekeeper.minimum is newer than running, the version of the Queuekeeper this
    process loaded, with how to update it; otherwise None. running is the version its pyproject.toml states, which a
    release's commit sets: at a release tag that release, and on main the newest release main holds. A version that
    cannot be read, "unknown", meets any minimum, as nothing says it falls short."""
    minimum = config.get("queuekeeper", {}).get("minimum")
    if minimum is None or not RELEASE_VERSION.fullmatch(running or ""): return None
    numbers = lambda version: tuple(map(int, version.split(".")))
    if numbers(running) >= numbers(minimum): return None
    return (f"{config.get('name', 'this project')}'s {FILE} asks for Queuekeeper {minimum} or newer (queuekeeper."
            f"minimum), and the Queuekeeper at {toolkit} is {running}: update it with `git -C {toolkit} fetch --tags` "
            f"and then `git -C {toolkit} checkout v{minimum}`, or a newer release's tag. That checkout serves every "
            "project on this Mac")


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
    if not isinstance(settings, dict) or set(settings) - {"seconds", "action", "reason", "primaryControl", "notice"}:
        raise ValueError(f"{name}.stall supports seconds, action, reason, primaryControl and notice")
    if "notice" in settings and (not isinstance(settings["notice"], str) or not settings["notice"].strip()):
        raise ValueError(f"{name}.stall.notice must be the words shown when the stall bound stops the lane")
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


def unmoved_minutes(name, lane):
    """A lane's unmovedMinutes: how long the files its admitted work writes may go unchanged before `qk queue doctor`
    notes its holder as unmoved, the project's own bound for a lane its Mac's past holds would misjudge, such as a UI-test
    lane that writes little until a test bundle ends (vocaro/queuekeeper#70). A display bound only: nothing stops the
    lane for it."""
    if "unmovedMinutes" not in lane: return
    minutes = lane["unmovedMinutes"]
    if type(minutes) not in (int, float) or not 0 < minutes < float("inf") or round(minutes * 60) < 1:
        raise ValueError(f"{name}.unmovedMinutes must be a positive number of minutes")


def failed_tests_setting(name, lane):
    """A lane's failedTests: where the lane writes its results in JUnit XML, under its results folder
    (QUEUEKEEPER_RESULTS_DIR), the command that reruns one of its tests alone, with {test} for the test's name, and the
    paths its tests depend on, each a file or a directory ending in /, which the merge queue reads to rerun a failed
    test alone before a bisect, as it does a qk unittest lane's (vocaro/queuekeeper#109). A lane whose plugin names its
    failed tests and reruns one (hooks.Reruns), as a qk unity test lane's, gives its paths alone."""
    if "failedTests" not in lane: return
    settings = lane["failedTests"]
    if not isinstance(settings, dict) or set(settings) - {"results", "rerun", "paths"}:
        raise ValueError(f"{name}.failedTests needs results and rerun, and supports paths")
    if not {"results", "rerun"} & set(settings) and hooks.lane_reruns(lane) is not None:
        failed_tests_paths(name, settings)
        return
    if not {"results", "rerun"} <= set(settings):
        raise ValueError(f"{name}.failedTests needs results and rerun, and supports paths; a lane whose plugin names "
                         "its failed tests, as a qk unity test lane's, gives paths alone")
    results = settings["results"]
    if not isinstance(results, str) or not results.strip() or Path(results).is_absolute() or ".." in Path(results).parts:
        raise ValueError(f"{name}.failedTests.results is the JUnit XML file's path in the lane's results folder "
                         "(QUEUEKEEPER_RESULTS_DIR), relative and inside it")
    rerun = settings["rerun"]
    if not isinstance(rerun, list) or not rerun or any(not isinstance(word, str) or not word for word in rerun) \
            or not any("{test}" in word for word in rerun):
        raise ValueError(f"{name}.failedTests.rerun is an argv list, never shell source, that names {{test}}, the "
                         "failed test it reruns alone")
    failed_tests_paths(name, settings)


def failed_tests_paths(name, settings):
    """Refuse a lane's failedTests.paths unless it lists files, each a file or a directory ending in /."""
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


def plugin_keys(name, lane):
    """Check each key of a lane that a plugin adds (hooks.LANE_KEYS), by the plugin's own rule. A key of
    PLUGIN_LANE_KEYS whose plugin the project has not switched on is refused, naming the plugin."""
    for key in lane:
        if key in hooks.LANE_KEYS: hooks.LANE_KEYS[key].check(name, lane)
        elif key in PLUGIN_LANE_KEYS:
            raise ValueError(f"{name}.{key} is the {PLUGIN_LANE_KEYS[key]} plugin's key, and the project does not "
                             f"switch that plugin on: list {PLUGIN_LANE_KEYS[key]}, or a plugin that requires "
                             "it, in plugins")


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
# The Mac hardware plugin, removed with its lane key chips once labels did the same (vocaro/queuekeeper#224).
CHIPS_PLUGIN = "queuekeeper_mac_hardware"
# Why a lane's chips, and the plugin that gave it, are refused.
CHIPS_GONE = ("which kept a lane on a Mac with a listed chip, is gone from Queuekeeper: labels replace it. Give the Mac "
              "the chip's name as a label in its mac-settings.json, such as \"labels\": [\"apple-m5-max\"], and the lane "
              "the same \"labels\"; the user guide's What a Mac takes says how")
# Why queuekeeper.json's spelling and queue.spelling are refused. Spelling is no concern of Queuekeeper's: a project
# that wants a spelling check, a linter or a formatter check makes it a lane of its own and lists it in
# queue.preflight (vocaro/queuekeeper#69).
SPELLING_GONE = ("is no longer a Queuekeeper setting, and Queuekeeper has no spelling check: make the project's check "
                 "a lane of its own, list it in queue.preflight, and keep its exemptions where that check reads them; "
                 "the user guide's Preflight lanes says how")
# A project's launcher: its copy of this repository's scripts/qk, which runs Queuekeeper on the project it is in.
LAUNCHER = "scripts/qk"
# The first word of a lane command that names Queuekeeper's own command, which launched makes the selected
# toolkit's bin/qk as the lane starts (vocaro/queuekeeper#340).
COMMAND_WORD = "qk"
# The toolkit's docs check, the docs lane of a project that has none (implicit_command, lane_to_run).
DOCS = "docs"
IMPLICIT_DOCS = ["python3", LAUNCHER, "docs", "check"]
# What the toolkit's docs check asks of Queuekeeper, by whichever command runs it (implicit_command).
IMPLICIT_WORDS = {DOCS: ["docs", "check"]}
# Conventional prose only; configuration, source and test fixtures keep their code gates. guides/ and
# knowledge/ are no standard documentation folders, so a project that keeps documents there names them in
# queue.unread.paths (vocaro/queuekeeper#106).
DOCS_FILES = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"}
DOCS_FOLDERS = ("doc/", "docs/")
# The root agent instruction files, which the toolkit's docs check reads as it reads every root Markdown file, and
# whose AGENTS.md it holds under 32 KiB: documentation by the defaults while the docs lanes run that check
# (agent_doc). On 2026-11-23 removing MathMLLayout's CLAUDE.md, checked by its docs lane alone, waited for the full
# gate (vocaro/queuekeeper#255).
AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
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
# The longest timeout, in seconds, of a lane the merge queue's worker may run unadmitted as a quick check
# (quick_check): the minute a preflight lane or `qk docs check` may run so (merge_queue.DOCS_CHECK_LIMIT). On
# 2026-11-23 MathMLLayout's docs lane, `python3 scripts/eng docs check` with a 10 s timeout, waited 25m29s for
# admission behind whole-host work (vocaro/queuekeeper#255).
QUICK_LIMIT = 60


def conventional_doc(path):
    """Whether path is conventional prose, which queue.unread takes unless its defaults are false."""
    return path in DOCS_FILES or (path.startswith(DOCS_FOLDERS) and path.endswith((".md", ".markdown")))


def agent_doc(config, path):
    """Whether path is a root agent instruction file (AGENT_FILES) that the docs lanes check: queue.unread.lanes run
    the toolkit's docs check (toolkit_docs), which reads every root Markdown file, so that a batch changing only such
    a file runs that check in place of the gate, as for conventional prose. A project whose docs lane is its own
    command, or that runs no docs lane, keeps the code gate for them; one whose tests read them excludes them."""
    lanes = unread_lanes(config)
    return path in AGENT_FILES and lanes is not None and DOCS in lanes and toolkit_docs(config)


def toolkit_docs(config):
    """Whether config's docs lane is the toolkit's check of every Markdown file: no docs lane, so the implicit one, or
    one whose command ends in qk's `docs check` with no paths after it, by any path to qk (implicit_command) or the
    bare word qk (launched)."""
    lane = (config.get("lanes") or {}).get(DOCS)
    if lane is None: return True
    command = lane.get("command") if isinstance(lane, dict) else None
    return isinstance(command, list) and len(command) >= 3 and command[-2:] == ["docs", "check"] \
        and any(isinstance(word, str) and PurePosixPath(word).name == COMMAND_WORD for word in command[:-2])


def names_path(path, key):
    """Whether a queue setting's key names path: a directory ending in /, or the file itself."""
    return path.startswith(key) if key.endswith("/") else path == key


def unread_declared(queue):
    """queue.unread as the queue reads it: False, or an object of paths, exclude, lanes and defaults."""
    return queue.get("unread", {})


def unread(config, path):
    """Whether no code lane reads path, by main's queue.unread (unread_declared): a batch that changes only such paths
    runs the docs lanes in place of the gate, and main changing one the batch does not leaves a passed result standing.
    exclude wins over the Markdown defaults and paths. A path that no gate lane's inputs name is one too (unselected),
    unless queue.unread is false or excludes it."""
    return unread_taken(config, path) or unread_taken(config, path, inputs=True)


def unread_taken(config, path, inputs=False):
    """Whether queue.unread itself takes path, by its Markdown defaults and paths; or, with inputs, whether the gate
    lanes' inputs leave it to skip the gate (unselected), which queue.unread being false or excluding it forbids."""
    declared = unread_declared(config.get("queue", {}))
    if declared is False: return False
    if any(names_path(path, key) for key in declared.get("exclude", {})): return False
    if inputs: return unselected(config, path)
    if declared.get("defaults", True) and (conventional_doc(path) or agent_doc(config, path)): return True
    return any(names_path(path, key) for key in declared.get("paths", {}))


def skips_by_inputs(config, path):
    """Whether path skips the gate by the lanes' inputs alone, and not as a path queue.unread takes."""
    return unread_taken(config, path, inputs=True) and not unread_taken(config, path)


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
    """Whether pattern, a glob of a lane's inputs, matches the whole of path: * and ? stay within one directory and **
    spans any number of them."""
    return PurePosixPath(path).full_match(pattern)


# The files every gate lane reads, whatever its inputs say: they hold the lanes' own commands and the checkout's
# composition (vocaro/queuekeeper#289).
EVERY_LANE = ("queuekeeper.json", "queuekeeper-workspace.json")


# When the queue's worker gates main whole for the landings whose gates their paths narrowed, where main's
# queue.fullRun does not say: once this many of them are covered by no whole gate since, or the oldest has waited
# this many minutes (vocaro/queuekeeper#289).
FULL_RUN = {"landings": 5, "minutes": 240}


def full_run(config):
    """main's queue.fullRun as the worker reads it: {"landings": N, "minutes": M}, or None where it is false."""
    declared = config.get("queue", {}).get("fullRun", {})
    return None if declared is False else {**FULL_RUN, **declared}


def lane_inputs(config):
    """{gate lane: its inputs} for the gate lanes (gate_lanes) of main that give inputs, the globs of the paths that
    affect each: a batch runs such a lane only when one of its changed paths matches one (vocaro/queuekeeper#289).
    A lane group that gives none of its own has those of its lanes together, once each of them gives inputs
    (member_inputs): one that gives none runs for every change, and so does its group."""
    lanes, found = config.get("lanes", {}), {}
    for name in gate_lanes(config):
        lane = lanes.get(name)
        if not isinstance(lane, dict): continue
        if "inputs" in lane: found[name] = lane["inputs"]
        elif "parallel" in lane:
            members = member_inputs(config, name)
            if len(members) == len(lane["parallel"]):
                found[name] = list(dict.fromkeys(pattern for patterns in members.values() for pattern in patterns))
    return found


def member_inputs(config, name):
    """{lane: its inputs} for the lanes of the lane group name that give inputs; none for a lane that is no group."""
    lanes = config.get("lanes", {})
    group = lanes.get(name)
    return {member: lanes[member]["inputs"] for member in (group.get("parallel", ()) if isinstance(group, dict) else ())
            if isinstance(lanes.get(member), dict) and "inputs" in lanes[member]}


def selected_members(config, name, changed):
    """The lanes of the gate lane group name that a batch changing the paths changed runs, in the group's order, or
    None for all of them: those whose inputs one of its paths matches, and those that give none. Every one when a
    path that selects every gate lane changed (affected_by), when no lane of the group gives inputs, or when the
    paths select them all anyway."""
    lanes = config.get("lanes", {})
    group, inputs = lanes.get(name), member_inputs(config, name)
    if not inputs or not changed or any(path in EVERY_LANE for path in changed): return None
    # A path that selects every gate lane, as one queue.unread says code reads and no lane names.
    if lane_inputs(config) and any(affected_by(config, path) is None for path in changed): return None
    chosen = [member for member in group["parallel"] if member not in inputs
              or any(matches(path, pattern) for path in changed for pattern in inputs[member])]
    return None if len(chosen) == len(group["parallel"]) else chosen


def unselected(config, path):
    """Whether path affects no gate lane, by the lanes' inputs (lane_inputs): every gate lane gives inputs, none of
    them matches path, and path is not one every lane reads (EVERY_LANE). A batch of such paths skips the gate."""
    inputs, named = lane_inputs(config), gate_lanes(config)
    return bool(named) and len(inputs) == len(named) and path not in EVERY_LANE \
        and not any(matches(path, pattern) for patterns in inputs.values() for pattern in patterns)


def selects_by(config):
    """What of main's settings narrows a batch's gate lanes by its paths, as a line says it after "main's": "lane
    inputs", or None when every batch runs every gate lane."""
    return "lane inputs" if lane_inputs(config) else None


def affected_by(config, path):
    """The gate lanes main selects for path, a changed path, by the lanes' inputs (lane_inputs), or None for every
    one: the lanes whose inputs match it and those that give none, which every change runs; none for a path
    queue.unread takes or no lane's inputs name (unselected); and None for one every lane reads (EVERY_LANE), or
    that queue.unread excludes or is false for while no lane's inputs name it, since that says code reads it. None
    too where no gate lane gives inputs."""
    inputs = lane_inputs(config)
    if not inputs or path in EVERY_LANE: return None
    if unread(config, path): return set()
    found = {name for name, patterns in inputs.items() if any(matches(path, pattern) for pattern in patterns)}
    return (found | set(gate_lanes(config)) - set(inputs)) or None


def affected_lanes(config, changed):
    """The gate lanes (gate_lanes) a batch changing the paths changed runs, in their order: those its paths select by
    main's gate lanes' inputs (affected_by), every one when a path that selects every lane changed, when no lane
    gives inputs, or when no path selects any, as a batch of unread paths alone, which runs the
    docs lanes instead."""
    named = gate_lanes(config)
    selected = set()
    for path in changed:
        lanes = affected_by(config, path)
        if lanes is None: return named
        selected |= lanes
    return [name for name in named if name in selected] or named


def unread_retired(queue):
    """Refuse queue.docsOnly and queue.carryOver, the older settings queue.unread replaced (vocaro/queuekeeper#189).
    docsOnly named the paths a batch could change and skip the gate, and carryOver the paths main could change under a
    passed result; both said a path no code lane reads, in two lists that could disagree. One release read them as
    queue.unread, and vocaro/vocaro-engineering#504 moved every project, so a checkout that still gives one is older
    than its project's main. The refusal says the queue.unread that reads the same, or the choice where none does."""
    given = [key for key in ("docsOnly", "carryOver") if key in queue]
    if not given: return
    said = " and ".join(f"queue.{key}" for key in given)
    went = f"queuekeeper.json gives {said}, which queue.unread replaced (vocaro/queuekeeper#189)"
    docs, carried = queue.get("docsOnly"), queue.get("carryOver") or {}
    if not isinstance(carried, dict) or not (docs is None or docs is False or isinstance(docs, dict)
                                             and isinstance(docs.get("paths"), dict)):
        raise ValueError(f"{went}: move each path into queue.unread.paths, with why no code lane reads it")
    if docs is False and carried:
        raise ValueError(f"{went}, which cannot say both that every batch gates and that "
                         f"{', '.join(sorted(carried))} carr{'ies' if len(carried) == 1 else 'y'} over: give "
                         f"\"queue\": {{\"unread\": false}} to carry nothing over, or {{\"unread\": {{\"paths\": "
                         f"{json.dumps(carried, sort_keys=True)}, \"defaults\": false}}}} to let those paths also "
                         "skip the gate")
    if docs is False: declared = False
    elif docs is None: declared = {"paths": dict(carried)}
    else: declared = {"paths": {**docs["paths"], **carried}, "defaults": False,
                      **({"exclude": docs["exclude"]} if docs.get("exclude") else {}),
                      **({"lanes": docs["lanes"]} if docs.get("lanes") not in (None, [DOCS]) else {})}
    raise ValueError(f"{went}: give \"queue\": {{\"unread\": {json.dumps(declared, sort_keys=True)}}} in its place, "
                     "which reads the same but that Markdown outside doc/ and docs/ carries over only if paths names it")


def unrun_carry_checks(config):
    """qk doctor's warning for each of queue.carryOverChecks that can never run: every path it is limited to is one
    queue.unread.paths names lanes for, which never carries over (merge_queue.carried_over), so main moving it gates
    again rather than running the check. A batch of such paths runs those lanes instead (vocaro/queuekeeper#27, #189)."""
    declared = unread_declared(config.get("queue", {}))
    if not isinstance(declared, dict): return []
    warnings = []
    for check in config.get("queue", {}).get("carryOverChecks", []):
        paths = check.get("paths")
        if not paths or not all(unread(config, path) and unread_named(config, [path])
                                and not any(names_path(key, path) for key in declared.get("exclude", {}))
                                for path in paths): continue
        warnings.append(f"queue.carryOverChecks runs {shlex.join(check['command'])} only on a move of "
                        f"{', '.join(paths)}, which never carries over, since queue.unread.paths names the lanes "
                        "that read it: main gaining it gates again and those lanes run on a batch of it, so the check "
                        "never runs; drop it, or name its lane there")
    return warnings


def quick_check(lane):
    """Why lane cannot run unadmitted as a quick check, listed in queue.quick or as the project's own docs lane (rule 28
    of the developer guide's scheduling rules, widened by the owner's decision of 2026-11-24, vocaro/queuekeeper#255),
    or None when it can: a light lane of one command (NOT_PREFLIGHT) whose timeout is QUICK_LIMIT seconds or less."""
    if not isinstance(lane, dict) or not isinstance(lane.get("command"), list) or not lane["command"]:
        return "it is not one command"
    keys = [key for key in (*NOT_PREFLIGHT, *hooks.LANE_KEYS) if lane.get(key)]
    if keys: return f"it is not one command: it has {', '.join(keys)}"
    if lane.get("resource") != "light": return 'it is not "resource": "light"'
    timeout = lane.get("timeout")
    if type(timeout) is not int or timeout > QUICK_LIMIT:
        return f"its timeout is not {QUICK_LIMIT} seconds or less"
    return None


def quick(config):
    """The lanes queue.quick names, which the merge queue's worker runs unadmitted as quick checks (quick_check)."""
    return list(config.get("queue", {}).get("quick", []))


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
    requires a pass of at the release commit; maxCommits and maxDays, the code commits and days since a lane's
    last pass on main after which the toolkit's checkpoint calls it due; and limits, a release lane's own
    maxCommits or maxDays in place of those. Queuekeeper applies no default to either: one left out is the
    toolkit's to choose. A name in lanes that is no lane is qk doctor's warning, not an error.
    A release lane that a landing also runs is an error: a release check runs once, on the release
    commit, and no change waits for one to land."""
    if not isinstance(declared, dict) or set(declared) - {"lanes", "maxCommits", "maxDays", "limits"}:
        raise ValueError("release supports only lanes, maxCommits, maxDays and limits")
    named = declared.get("lanes", [])
    if not isinstance(named, list) or any(not isinstance(name, str) or not name.strip() for name in named) \
            or len(set(named)) != len(named) or "gate" in named:
        raise ValueError("release.lanes must list distinct lane names, never gate, which eng release checks first")
    for key in ("maxCommits", "maxDays"):
        if key in declared and (type(declared[key]) is not int or declared[key] <= 0):
            raise ValueError(f"release.{key} must be a positive integer")
    limits = declared.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("release.limits must map release lanes to their own maxCommits or maxDays")
    for name, limit in limits.items():
        if name not in named:
            raise ValueError(f"release.limits names {name}, which is not in release.lanes")
        if not isinstance(limit, dict) or set(limit) - {"maxCommits", "maxDays"}:
            raise ValueError(f"release.limits.{name} supports only maxCommits and maxDays")
        for key, value in limit.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"release.limits.{name}.{key} must be a positive integer")
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


# A literal repository-relative file name with no empty, dot or parent components.
ADD_ONLY_JSON_PATH = re.compile(r"(?![.]{1,2}(?:/|$))(?:[^/\\:\x00-\x1f]+/(?!(?:[.]{1,2})(?:/|$)))*[^/\\:\x00-\x1f]*[.]json")


def merge_queue(queue, lanes):
    """The lanes a commit passes in the merge queue, what main may change under a passed result,
    each path with why no such lane reads it, the commands that main's change must pass first, and
    which of the lanes cover the pre-push hook, each with what of the hook it runs, the paths a
    docs-only batch changes, each with why no code lane reads it, with the lanes it runs instead, and how
    many runs in a row may give no verdict before the queue holds.
    unread, the paths no code lane reads, replaced docsOnly and carryOver, which are refused (unread_retired)."""
    if isinstance(queue, dict) and "spelling" in queue: raise ValueError(f"queue.spelling {SPELLING_GONE}")
    if isinstance(queue, dict):
        unread_retired(queue)
        affected_retired(queue)
    if not isinstance(queue, dict) or set(queue) - {"lanes", "carryOverChecks", "hookCoveredBy", "unread",
                                                    "noVerdictRuns", "followLandings", "preflight", "quick", "fullRun", "addOnlyJson",
                                                    "reuseGate"}:
        raise ValueError("queue supports only lanes, unread, carryOverChecks, hookCoveredBy, noVerdictRuns, "
                         "followLandings, preflight, quick, fullRun, addOnlyJson and reuseGate")
    added = queue.get("addOnlyJson", {})
    if not isinstance(added, dict) or any(not isinstance(path, str) or not ADD_ONLY_JSON_PATH.fullmatch(path)
                                         or not isinstance(keys, list) or any(not isinstance(key, str) for key in keys)
                                         for path, keys in added.items()):
        raise ValueError("queue.addOnlyJson maps repository-relative .json files to arrays of object keys, "
                         "an empty array for the file's root object")
    if "fullRun" in queue:
        declared = queue["fullRun"]
        if declared is not False and (not isinstance(declared, dict) or not declared or set(declared) - set(FULL_RUN)
                                      or any(type(value) is not int or value < 1 for value in declared.values())):
            raise ValueError("queue.fullRun is false, never to gate main whole after landings whose gates their paths "
                             "narrowed, or an object of landings, how many of them may wait for it, and minutes, how "
                             f"long the oldest may, each a positive integer ({FULL_RUN['landings']} and "
                             f"{FULL_RUN['minutes']} where unset)")
    if "unread" in queue:
        if queue["unread"] is not False and not unread_object(queue["unread"], lanes):
            raise ValueError("queue.unread is false to gate every batch and carry nothing over, or an object of "
                             "optional paths, mapping each path, a directory ending in /, to why no code lane "
                             "reads it, or to an object of that why and lanes, the distinct configured lanes, never "
                             "gate, of the few checks that do read it, which a batch changing it runs too; "
                             "exclude, the paths among them or the Markdown defaults that code does read, each with "
                             "why; lanes, the distinct configured lanes a batch of unread paths runs instead, never "
                             "gate; and defaults, false to drop the Markdown defaults")
    if "preflight" in queue: preflight_lanes(queue, lanes)
    if "quick" in queue: quick_setting(queue, lanes)
    if "followLandings" in queue and type(queue["followLandings"]) is not bool:
        raise ValueError("queue.followLandings is true or false: whether the queue fast-forwards the project's main "
                         "checkout after each landing; true where it is unset")
    if "noVerdictRuns" in queue and (type(queue["noVerdictRuns"]) is not int or queue["noVerdictRuns"] < 1):
        raise ValueError("queue.noVerdictRuns is how many runs in a row may give no verdict before the batch comes back, "
                         "a positive integer")
    named = queue.get("lanes", [])
    if not isinstance(named, list) or len(set(map(str, named))) != len(named) or any(name not in lanes for name in named):
        raise ValueError("queue.lanes must list distinct configured lanes")
    reuse = queue.get("reuseGate", {})
    if not isinstance(reuse, dict) or any(
        lane not in lanes or lane not in (named or ["gate"]) or lane == PROVISION
        or not isinstance(probe, list) or not probe or any(not isinstance(word, str) or not word for word in probe)
        for lane, probe in reuse.items()
    ):
        raise ValueError("queue.reuseGate maps configured gate lanes to nonempty input-probe commands (argv lists)")
    if "hookCoveredBy" in queue:
        covering = queue["hookCoveredBy"]
        passed = {*preflight({"lanes": lanes, "queue": queue}), PROVISION, *(named or ["gate"])} & set(lanes)
        if not isinstance(covering, dict) or not covering or any(lane not in passed or not isinstance(what, str)
                                                                 or not what.strip() for lane, what in covering.items()):
            raise ValueError("queue.hookCoveredBy maps each lane the queue runs that covers the pre-push hook to what "
                             "of the hook it runs")
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
        # A preflight lane with no timeout runs under PREFLIGHT_LIMIT, not the default lane_timing.check judged against.
        target = lanes[name].get("targetMinutes")
        if "timeout" not in lanes[name] and isinstance(target, (int, float)) and not isinstance(target, bool) \
                and target * 60 > PREFLIGHT_LIMIT:
            raise ValueError(f"{name}.targetMinutes {target:g} is past the {PREFLIGHT_LIMIT}s preflight limit {name} "
                             f"runs under, because queue.preflight lists it and it gives no timeout: lower the target "
                             f"or give the lane a longer timeout")


def quick_setting(queue, lanes):
    """queue.quick, checked: distinct configured lanes, none of them the gate, provision or one of queue.lanes, each a
    quick check (quick_check) that the merge queue's worker may run unadmitted (vocaro/queuekeeper#255)."""
    named = queue["quick"]
    if not isinstance(named, list) or any(not isinstance(name, str) or not name.strip() for name in named) \
            or len(set(named)) != len(named):
        raise ValueError("queue.quick lists distinct lane names, the light checks of one command and a timeout of "
                         f"{QUICK_LIMIT} seconds or less that the merge queue runs unadmitted; [] for none")
    for name in named:
        if name not in lanes: raise ValueError(f"queue.quick names {name}, which is no configured lane")
        if name in ("gate", PROVISION) or name in (queue.get("lanes") or []):
            raise ValueError(f"queue.quick names {name}, which is a gate lane or provision, and those always take "
                             "admission")
        why = quick_check(lanes[name])
        if why:
            raise ValueError(f"queue.quick names {name}, which is not a quick check that the merge queue can run "
                             f'unadmitted: {why}. A quick check is "resource": "light", one command, with a timeout '
                             f"of {QUICK_LIMIT} seconds or less")


def affected_retired(queue):
    """Refuse queue.affected, which each gate lane's inputs replaced (vocaro/queuekeeper#289). It mapped globs to the
    gate lanes that read them, the same map written from the paths, with one rule the other way: a path no rule
    covered ran every gate lane. No project's main gave it when it went (read from GitHub, 2026-11-20), so a checkout
    that gives it is no project's main. The refusal says the inputs that select the same lanes for the same paths."""
    if "affected" not in queue: return
    went = "queuekeeper.json gives queue.affected, which each gate lane's inputs replaced (vocaro/queuekeeper#289)"
    rules, inputs = queue["affected"], {}
    try:
        for rule in rules:
            for name in rule["lanes"]:
                for pattern in rule["paths"]:
                    if pattern not in inputs.setdefault(name, []): inputs[name].append(pattern)
    except (TypeError, KeyError):
        raise ValueError(f"{went}: give each gate lane \"inputs\", the globs of the paths that affect it") from None
    said = ", ".join(f"{json.dumps(name)}: {json.dumps(patterns)}" for name, patterns in inputs.items())
    raise ValueError(f"{went}: delete it and give each gate lane its \"inputs\" in its place, {said}. A path none of "
                     "them names then runs no gate lane, where under queue.affected it ran every one, so add what "
                     "each lane reads besides, and read qk doctor's affectedPaths.skipping")


def lane_inputs_checked(queue, lanes):
    """Each lane's inputs, checked: distinct globs of the paths that affect a gate lane (gate_lanes) or a lane of a
    gate lane group, which no other lane takes, and never both for a group and for one of its lanes."""
    given = [name for name, lane in lanes.items() if isinstance(lane, dict) and "inputs" in lane]
    if not given: return
    queue = queue if isinstance(queue, dict) else {}
    named = gate_lanes({"lanes": lanes, "queue": queue})
    for name in given:
        patterns = lanes[name]["inputs"]
        if not isinstance(patterns, list) or not patterns or len(set(map(str, patterns))) != len(patterns) \
                or any(not isinstance(pattern, str) or not pattern.strip() for pattern in patterns):
            raise ValueError(f"{name}.inputs lists distinct globs of the paths that affect the lane (* within one "
                             "directory, ** across them): a batch runs the lane only when it changes one")
        for pattern in patterns:
            if pattern.startswith("/") or not PurePosixPath(pattern).parts:
                raise ValueError(f"{name}.inputs' glob {pattern} is no path relative to the repository")
        groups = [group for group in named if isinstance(lanes.get(group), dict)
                  and name in lanes[group].get("parallel", ())]
        if name not in named and not groups:
            raise ValueError(f"{name}.inputs is given for a lane that is not one of the gate lanes "
                             f"({', '.join(named) or 'none'}), queue.lanes or else gate, nor a lane of a gate lane "
                             "group: only those are chosen by a batch's paths")
        both = next((group for group in groups if "inputs" in lanes[group]), None)
        if both:
            raise ValueError(f"{both}.inputs and {name}.inputs are both given, for a lane group and for one of its "
                             f"lanes: give the group's to run all of its lanes or none, or its lanes' to choose "
                             "among them")


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
    return queuekeeper_command(root, words, toolkit)


def qk_words(root=None, anywhere=False):
    """The words that start Queuekeeper in the project at root, as a message tells a person or a session to run it:
    `python3 scripts/qk` when the project holds its launcher, else the installed `qk`. With anywhere, for a command
    run from any directory, they name root: `python3 ROOT/scripts/qk`, else `qk --project ROOT`. root is by default
    the project this runs in, the nearest directory at or above the current one that holds a launcher or a .git.
    A project may drop its copy of the launcher (vocaro/queuekeeper#20), and then a message that named
    `python3 scripts/qk`, as the disk-full hold did, named a file it does not have (vocaro/queuekeeper#339)."""
    if root is None:
        try: here = Path.cwd()
        except OSError: return [COMMAND_WORD]
        root = next((folder for folder in (here, *here.parents)
                     if (folder / LAUNCHER).is_file() or (folder / ".git").exists()), None)
        if root is None: return [COMMAND_WORD]
    root = Path(root)
    if (root / LAUNCHER).is_file(): return ["python3", str(root.resolve() / LAUNCHER) if anywhere else LAUNCHER]
    return [COMMAND_WORD, "--project", str(root.resolve())] if anywhere else [COMMAND_WORD]


def qk_said(words, root=None, anywhere=False):
    """The command line that runs Queuekeeper's words, a string such as "disk --delete" or a list of words, in the
    project at root, as a message names it (qk_words)."""
    words = shlex.split(words) if isinstance(words, str) else list(words)
    return shlex.join([*qk_words(root, anywhere), *words])


def queuekeeper_command(root, words, toolkit=None):
    """The command that runs Queuekeeper's words on the project at root by the toolkit's own bin/qk, with the Python
    that runs this Queuekeeper, and the project named: the toolkit is toolkit, else the one QUEUEKEEPER_ROOT names,
    as a launcher reads it, else this one."""
    root = Path(root)
    named = toolkit or os.environ.get("QUEUEKEEPER_ROOT")
    kit = (root / named).resolve() if named else TOOLKIT_ROOT
    return [sys.executable, str(kit / "bin/qk"), "--project", str(root.resolve()), *words]


def launched(command, root, toolkit=None):
    """The command that starts a lane's configured command in the project at root: one whose first word is the bare
    word COMMAND_WORD, as ["qk", "unittest", ...], runs the rest by the selected toolkit's bin/qk with the project
    named (queuekeeper_command), and any other command runs as it is. The bare word always means Queuekeeper's own
    command, never a project's scripts/qk copy, so a lane runs the toolkit the run selected. qk run, the queue's
    reruns and its preflight runs make it as the command starts, on the Mac that runs it, so that a lane sent to
    another Mac names that Mac's toolkit; receipts and the readings of a lane keep the configured words. A project
    retiring its copy of the launcher (vocaro/queuekeeper#20) needs no wrapper of its own for lanes, which, named
    anything but qk, silently lost what reads Queuekeeper's command by that name (vocaro/queuekeeper#340)."""
    if isinstance(command, list) and command[:1] == [COMMAND_WORD]:
        return queuekeeper_command(root, command[1:], toolkit)
    return command


def queuekeeper_at(command, subcommand):
    """The index in command of subcommand as the Queuekeeper subcommand it runs, or None: subcommand right after a
    word whose name is qk (the bare word, bin/qk or a project's launcher), or after that word's --project option, as
    launched and queuekeeper_command place it. What reads a lane as qk unittest or qk docs check reads it so."""
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command): return None
    for index, word in enumerate(command[:-1]):
        if PurePosixPath(word).name != COMMAND_WORD: continue
        at = index + 1
        if command[at] == "--project": at += 2
        elif command[at].startswith("--project="): at += 1
        if at < len(command) and command[at] == subcommand: return at
    return None


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
    Mac first: provision prepares the checkout in hand, a self lane runs where it starts, and a lane or a lane
    of a group may say why with "here". A group runs its lanes under its one admission on one Mac, so a lane
    of it that stays here keeps the group here. A light lane is placed as every lane is (the owner's decision,
    2026-11-07). Foreground lanes also follow their host's foreground settings (foreground.py)."""
    lane = lanes[name]
    if name == PROVISION: return "the provision lane prepares this checkout"
    if unadmitted(lane): return "a self lane runs where it starts, and its script admits each of its parts"
    if lane.get("here"): return f"the lane says: {lane['here']}"
    for member in lane.get("parallel", []):
        if lanes[member].get("here"): return f"its lane {member} says: {lanes[member]['here']}"
    return None


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
                         "unlockedScreen, labels, failFast, disk and budgetMinutes")
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
    disk_settings(f"{name}.disk", lane.get("disk", {}))
    model(name, lane)
    whole_host(name, lane)
    screen(name, lane)


def disk_settings(where, value):
    """The project's, a lane's or a group's "disk": {"startFreeGB": N}, the GiB free a lane that fills the disk waits
    for before admission (start_free): a number at or above 0, where 0 waits for none (vocaro/queuekeeper#288)."""
    if not isinstance(value, dict) or set(value) - {"startFreeGB"}:
        raise ValueError(f"{where} takes only startFreeGB")
    if "startFreeGB" in value:
        floor = value["startFreeGB"]
        if type(floor) not in (int, float) or not floor >= 0:
            raise ValueError(f"{where}.startFreeGB must be a number of GiB at or above 0")


def start_free(config, name, lane=None):
    """The GiB free that lane name (lane, else config's) waits for before admission when it fills the disk, as the
    project's settings give it, with where it comes from: (GiB, where) from the lane's disk.startFreeGB, else the
    project's, else None for disk.START_FREE's default. QUEUEKEEPER_START_FREE_GB overrides either (disk.free_floor).
    The floor was one for every project, sized for Xcode builds, and kept a Unity lane that writes little waiting over
    an hour (vocaro/queuekeeper#288)."""
    lane = lane if lane is not None else config.get("lanes", {}).get(name)
    own = lane.get("disk", {}) if isinstance(lane, dict) else {}
    if "startFreeGB" in own: return own["startFreeGB"], f"lanes.{name}.disk.startFreeGB in {FILE}"
    if "startFreeGB" in config.get("disk", {}): return config["disk"]["startFreeGB"], f"disk.startFreeGB in {FILE}"
    return None


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
