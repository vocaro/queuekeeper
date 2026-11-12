"""Small, strict project configuration without third-party dependencies."""
import json
import os
import re
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parents[3]
# integrations/shared.py, copied into each project, keeps its own copy of this path.
DEFAULT_TOOLKIT = "../vocaro-engineering"
CAPABILITIES = {
    "simulator-pool", "voiceover", "background-mac",
    "packaging", "measurement", "native-python", "external-validation",
    "background-processing", "app-extensions", "data-protection", "file-handoff",
    "storekit", "foundation-models", "background-assets",
}


def load(root):
    return parse((Path(root).resolve() / "engineering.json").read_text())


# The keys a lane and a lane group take, which parse checks and schemas/engineering.schema.json lists (a test
# compares them, vocaro-engineering#248).
LANE_KEYS = frozenset({"command", "resource", "minimumTests", "timeout", "foreground", "here", "model", "display",
                       "budgetMinutes", "wholeHost", "apps", "unlockedScreen", "devices", "compileFirst",
                       "deviceRunMinutes", "stall"})
GROUP_KEYS = frozenset({"parallel", "jobs", "resource", "here", "model", "budgetMinutes", "wholeHost", "unlockedScreen"})


def device_keys(name, lane):
    """A lane's "devices", the entries of the device record its run uses, which a device pass's own --device options
    say without it. Only an admitted lane waits for admission, which they bound (lane_devices)."""
    if "devices" not in lane: return
    keys = lane["devices"]
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError(f"{name}.devices must be a non-empty list of device entries, such as \"iPhone\"")
    if unadmitted(lane):
        raise ValueError(f"{name}.devices needs an admitted lane: it bounds the wait for admission, and a self lane "
                         "is not admitted")


def lane_devices(lane):
    """The device-record entries a lane's run uses (vocaro-engineering#292): its "devices", for a project's script
    that calls device_pass.run, or each --device of an `queuekeeper device-pass` command, up to that command's own --, which
    the words of the command it runs follow. Any other lane uses none."""
    if lane.get("devices"): return list(lane["devices"])
    command = lane.get("command") or []
    if "device-pass" not in command: return []
    words = command[command.index("device-pass") + 1:]
    if "--" in words: words = words[:words.index("--")]
    return [word.split("=", 1)[1] if word.startswith("--device=") else words[index + 1]
            for index, word in enumerate(words) if word.startswith("--device=")
            or (word == "--device" and index + 1 < len(words))]


def parse(source):
    """A project's engineering.json, checked, from its text, wherever that came from."""
    value = json.loads(source)
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("engineering.json requires schemaVersion 1")
    allowed = {"schemaVersion", "name", "toolkit", "capabilities", "lanes", "exceptions",
               "integration", "repository", "spelling", "simulatorPool", "githubArtifacts", "queue", "release", "app",
               "testLanes"}
    hosted = [key for key in ("hostedRepositories", "voiceover") if key in value]
    if hosted:
        raise ValueError(f"engineering.json gives {' and '.join(hosted)}, which configured GitHub's hosted Mac runner "
                         "until vocaro-engineering stopped running tests there: Mac VoiceOver runs on the second Mac "
                         "as a foreground lane, so delete them, and rebase a checkout older than its project's change "
                         "onto main (guides/second-mac-foreground.md)")
    if set(value) - allowed:
        raise ValueError(f"Unknown configuration keys: {sorted(set(value) - allowed)}")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("Project name must be a nonempty string")
    toolkit = value.get("toolkit", {})
    if not isinstance(toolkit, dict) or set(toolkit) - {"path"} or not isinstance(toolkit.get("path", DEFAULT_TOOLKIT), str):
        raise ValueError("toolkit must contain only an optional checkout path")
    if value.get("integration", "direct-main") not in {"direct-main", "pull-request"}:
        raise ValueError("Unknown integration policy")
    artifacts = value.get("githubArtifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(name, str) or
            not re.fullmatch(r"\.|[A-Za-z0-9_-]+", name) for name in artifacts) or len(set(artifacts)) != len(artifacts):
        raise ValueError("githubArtifacts must list distinct package identities, or . for this package")
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities):
        raise ValueError("capabilities must be a list of names")
    if "simulator-disposable" in capabilities:
        raise ValueError("simulator-disposable is retired: use simulator-pool and name the pool in simulatorPool "
                         "(guides/simulator-testing.md, Reserved simulator pool)")
    if set(capabilities) - CAPABILITIES:
        raise ValueError(f"Unknown capabilities: {sorted(set(capabilities) - CAPABILITIES)}")
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("Duplicate capability")
    pool = value.get("simulatorPool")
    if ("simulator-pool" in capabilities) != (pool is not None):
        raise ValueError("The simulator-pool capability and simulatorPool, the name its members start with, go together")
    if pool is not None and (not isinstance(pool, str) or not pool.strip() or pool != pool.strip()):
        raise ValueError("simulatorPool must be a nonempty name, such as the project's")
    if not isinstance(value.get("lanes", {}), dict):
        raise ValueError("lanes must be an object")
    lanes = value.get("lanes", {})
    retired(lanes)
    for name, lane in lanes.items():
        budget(name, lane)
        if isinstance(lane, dict) and "parallel" in lane:
            group(name, lane, lanes)
            continue
        if not isinstance(lane, dict) or set(lane) - LANE_KEYS:
            raise ValueError(f"Invalid lane {name}")
        apps(name, lane)
        device_keys(name, lane)
        for key in ["foreground", "model", "display", "wholeHost", "unlockedScreen"]:
            if key in lane and type(lane[key]) is not bool:
                raise ValueError(f"{name}.{key} must be true or false")
        resource_of(name, lane)
        if lane.get("foreground") and unadmitted(lane):
            raise ValueError(f"{name}.foreground needs an admitted lane: the second Mac's foreground is held inside the "
                             "run's admission, and a self lane is not admitted")
        place(name, lane)
        model(name, lane)
        whole_host(name, lane)
        screen(name, lane)
        compile_first(name, lane)
        stall(name, lane)
        if lane.get("display") and unadmitted(lane):
            raise ValueError(f"{name}.display needs an admitted lane: only an admitted test run holds the display's "
                             "assertions, and a self lane is not admitted")
        command = lane.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f"Lane {name} needs an argv list, never shell source")
        for key in ["minimumTests", "timeout"]:
            if key in lane and (type(lane[key]) is not int or lane[key] <= 0):
                raise ValueError(f"{name}.{key} must be a positive integer")
        if "deviceRunMinutes" in lane:
            minutes = lane["deviceRunMinutes"]
            if type(minutes) is not int or minutes <= 0:
                raise ValueError(f"{name}.deviceRunMinutes must be a positive integer")
            if not lane_devices(lane) or unadmitted(lane):
                raise ValueError(f"{name}.deviceRunMinutes needs an admitted device lane")
    merge_queue(value.get("queue", {}), lanes)
    release(value.get("release", {}), lanes, value.get("queue", {}))
    if "app" in value: app(value["app"])
    spelling = value.get("spelling", {})
    if not isinstance(spelling, dict) or set(spelling) - {"skip", "exemptPhrases", "exemptRuns", "exemptTokens"}:
        raise ValueError("spelling supports only skip, exemptPhrases, exemptRuns and exemptTokens")
    for key, entries in spelling.items():
        if not isinstance(entries, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(r, str)
                                                or not r.strip() for k, r in entries.items()):
            raise ValueError(f"spelling.{key} maps each entry to the reason it is kept")
    test_lanes(value.get("testLanes", {}))
    exceptions = value.get("exceptions", [])
    if not isinstance(exceptions, list) or any(not isinstance(x, dict) or not all(isinstance(x.get(k), str) and x[k] for k in ["rule", "reason"]) for x in exceptions):
        raise ValueError("Exceptions require a rule and a reason")
    return value


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


def test_lanes(declared):
    """What queuekeeper doctor's lane coverage (lane_coverage.py, #289) cannot work out for itself: skip, the test paths kept as
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
    on the derived data the command uses, so that a compile error stops the run before its tests take a simulator
    (guides/app-testing.md, Compile before the tests). A self lane has no admission of its own to run it in."""
    if "compileFirst" not in lane: return
    command = lane["compileFirst"]
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError(f"{name}.compileFirst needs an argv list, never shell source")
    if unadmitted(lane):
        raise ValueError(f"{name}.compileFirst needs an admitted lane: it runs inside the lane's one admission, and a "
                         "self lane is not admitted")


# What a lane takes of its Mac. Every lane is admitted, first come, first served, and offered to the second Mac
# first (the owner's decision, 2026-11-07, vocaro-engineering#200): heavy, one slot of heavy-work admission, which a
# lane that gives no resource takes too; light, which once ran unadmitted where it started, now means heavy, so
# that the engineering.json files that give it keep loading; exclusive, the host to itself; and self, a lane whose
# script admits each of its parts itself, such as a calibration that builds under `queuekeeper admit` and then times under
# `queuekeeper admit --exclusive`, which an admission of its own would refuse, so queuekeeper run runs it where it starts, unadmitted
# (guides/shared-host.md, Heavy-work admission).
RESOURCES = ["light", "heavy", "exclusive", "self"]
# How heavily a lane's admission holds its Mac, so that a group's is at least each of its lanes'.
WEIGHT = {"heavy": 0, "exclusive": 1}


def resource_of(name, lane):
    """The admission the lane or group takes: heavy, exclusive or self, light and no resource meaning heavy."""
    resource = lane.get("resource", "heavy")
    if resource not in RESOURCES:
        raise ValueError(f"Invalid resource for {name}: {resource!r} is none of {', '.join(RESOURCES)}")
    return "heavy" if resource == "light" else resource


# A call of queuekeeper admit, as a shell script or a command's own words write it (`scripts/qk admit`), or as a
# Python list of a command's words does (`[*queuekeeper, "admit", ...]`).
ADMIT_CALL = re.compile(r"""\b(?:qk|queuekeeper)\b["' ,\])]*admit\b|["']admit["']""")


def self_lane_warnings(root, config):
    """queuekeeper doctor's warning for each self lane whose command neither calls queuekeeper admit in its own words nor runs
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
                        f"but neither its command nor {ran} calls queuekeeper admit, so its work runs outside admission: "
                        "give it \"resource\": \"heavy\", or admit each part with queuekeeper admit (guides/shared-host.md)")
    return warnings


def display_lane_warnings(config):
    """queuekeeper doctor's warning for each "display": true lane that does not say "unlockedScreen": "display" wakes the
    display and keeps it on, behind a lock too, but unlocks nothing, so queuekeeper may place the lane on a Mac whose screen
    is locked, where a test that reports a lock as unmeasured gives no verdict, and the merge queue sends its gate
    there again until it holds (vocaro-engineering#293). A lane whose tests measure behind a lock says
    "unlockedScreen": false, which settles the question; only a lane that leaves it out is warned of."""
    lanes = config.get("lanes", {})
    warnings = []
    for name, lane in sorted(lanes.items()):
        if not isinstance(lane, dict) or not lane.get("display") or "unlockedScreen" in lane: continue
        groups = sorted(group for group, other in lanes.items() if isinstance(other, dict)
                        and name in (other.get("parallel") or []) and not other.get("unlockedScreen"))
        also = f", and so does each group that runs it, {', '.join(groups)}," if groups else ""
        warnings.append(f"{name} says \"display\": true, which wakes the display but does not unlock the screen, and "
                        "does not say \"unlockedScreen\", so queuekeeper may place it on a Mac whose screen is locked, where a "
                        "test that reports a lock as unmeasured gives no verdict: if its tests need an unlocked "
                        f"screen, it needs \"unlockedScreen\": true{also} and if they measure behind a lock, say "
                        "\"unlockedScreen\": false (guides/shared-host.md, Which Mac runs a lane)")
    return warnings


def unadmitted(lane):
    """Whether queuekeeper runs the lane, or group, without admission of its own: a self lane, whose script admits each of
    its parts."""
    return lane.get("resource") == "self"


def admission(lane):
    """The admission queuekeeper run takes for the lane, or group: heavy, exclusive, or self for none."""
    return resource_of("the lane", lane)
# The platforms `queuekeeper app` builds an app for, and whether each is kept after every gated landing unless the
# project says otherwise: the Mac build is, so that the newest landed app opens at once; a device build is
# made when a device asks for it (app_builds.py).
APP_PLATFORMS = {"mac": True, "ios": False}


def app(declared):
    """The app `queuekeeper app` keeps and runs (app_builds.py, guides/app-builds.md)."""
    if not isinstance(declared, dict) or set(declared) - {"project", "workspace", "directory", "configuration",
                                                          "prepare", *APP_PLATFORMS}:
        raise ValueError("app supports only project or workspace, directory, configuration, prepare, mac and ios")
    if ("project" in declared) == ("workspace" in declared):
        raise ValueError("app names its Xcode project or its workspace, one of them")
    for key in ("project", "workspace", "directory", "configuration"):
        if key in declared and (not isinstance(declared[key], str) or not declared[key].strip()):
            raise ValueError(f"app.{key} must be a nonempty string")
    for key in ("project", "workspace", "directory"):
        if key in declared and (Path(declared[key]).is_absolute() or ".." in Path(declared[key]).parts):
            raise ValueError(f"app.{key} is a path inside the checkout")
    prepare = declared.get("prepare")
    if prepare is not None:
        steps = prepare if isinstance(prepare, list) and prepare and all(isinstance(step, list) for step in prepare) \
            else [prepare]
        if any(not isinstance(step, list) or not step or any(not isinstance(word, str) or not word for word in step)
               for step in steps):
            raise ValueError("app.prepare needs an argv list, or a list of them run in order, never shell source")
    platforms = [name for name in APP_PLATFORMS if name in declared]
    if not platforms:
        raise ValueError("app names at least one platform to build: mac or ios")
    for name in platforms:
        platform = declared[name]
        if not isinstance(platform, dict) or set(platform) - {"scheme", "product", "settings", "afterLanding"}:
            raise ValueError(f"app.{name} supports only scheme, product, settings and afterLanding")
        if not isinstance(platform.get("scheme"), str) or not platform["scheme"].strip():
            raise ValueError(f"app.{name}.scheme must name the scheme that builds the app")
        product = platform.get("product")
        if product is not None and (not isinstance(product, str) or not product.endswith(".app") or "/" in product):
            raise ValueError(f"app.{name}.product is the built app's file name, such as Name.app")
        settings = platform.get("settings", {})
        if not isinstance(settings, dict) or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
                                                 or not isinstance(text, str) for key, text in settings.items()):
            raise ValueError(f"app.{name}.settings maps build setting names to text")
        if "afterLanding" in platform and type(platform["afterLanding"]) is not bool:
            raise ValueError(f"app.{name}.afterLanding must be true or false")


# The lanes the merge queue runs before its gate where a project has them: merge_queue.BEFORE_GATE, kept
# here too because merge_queue reads this module.
LANDING_FIRST = ["spelling", "provision"]
SPELLING = "spelling"
# The toolkit's spelling check, the spelling lane of a project that has none: the queue runs it before the gate, on an
# ungated landing and before a docs-only batch's lanes, unless main's queue.spelling is false, so that a British spelling
# fails in a minute, not after a full gate and a bisect (vocaro-engineering, 2026-11-07). queuekeeper run admits and places it
# as any lane (lane_to_run).
IMPLICIT_SPELLING = ["python3", "scripts/qk", SPELLING]
DOCS = "docs"
IMPLICIT_DOCS = ["python3", "scripts/qk", "docs", "check"]
# Conventional prose only; configuration, agent policy, source and test fixtures keep their code gates.
DOCS_FILES = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"}
DOCS_FOLDERS = ("doc/", "docs/", "guides/", "knowledge/")
# Seconds the spelling check may run, as that lane and before queuekeeper land queues an entry (vocaro-engineering#283).
SPELLING_LIMIT = 300


def landing_lanes(lanes, queue):
    """Each lane a landing runs, with why: the cheap checks before the gate, queue.lanes or gate, the lanes a
    docs-only batch runs instead, and every lane a group among them runs (guides/maintenance.md, The merge
    queue)."""
    named = queue.get("lanes") or (["gate"] if "gate" in lanes else [])
    found = {name: "the merge queue runs it before the gate" for name in LANDING_FIRST if name in lanes}
    found.update({name: "the merge queue runs it as " + ("one of queue.lanes" if queue.get("lanes") else "the gate")
                  for name in named})
    found.update({name: "a docs-only batch runs it, by the default or queue.docsOnly.lanes"
                  for name in (queue.get("docsOnly") or {}).get("lanes",
                      [DOCS] if "docsOnly" not in queue else []) if name not in found})
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
    requires a pass of at the release commit, and maxCommits, the checkpoint's limit (guides/releasing.md,
    Two gates and Checkpoints). A name in lanes that is no lane is queuekeeper doctor's warning, not an error.
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
                             "release.lanes (guides/releasing.md, Two gates)")


def budget(name, lane):
    """budgetMinutes, the gate's time budget, from its measured time, past which queuekeeper land and queuekeeper doctor warn
    (guides/releasing.md, Keeping the split). Only the gate lane has one."""
    if not isinstance(lane, dict) or "budgetMinutes" not in lane: return
    if name != "gate":
        raise ValueError(f"{name}.budgetMinutes: only the gate lane has a time budget (guides/releasing.md, "
                         "Keeping the split)")
    if type(lane["budgetMinutes"]) is not int or lane["budgetMinutes"] <= 0:
        raise ValueError("gate.budgetMinutes must be a positive integer")


def merge_queue(queue, lanes):
    """The lanes a commit passes in the merge queue, what main may change under a passed result,
    each path with why no such lane reads it, the commands that main's change must pass first, and
    which of the lanes cover the pre-push hook, each with what of the hook it runs, the paths a
    docs-only batch changes, each with why no code lane reads it, with the lanes it runs instead, and how
    many runs in a row may give no verdict before the queue holds, and spelling, false for a project whose queue
    runs no spelling check of the toolkit's in place of a spelling lane (guides/maintenance.md, The merge queue)."""
    if not isinstance(queue, dict) or set(queue) - {"lanes", "carryOver", "carryOverChecks", "hookCoveredBy", "docsOnly",
                                                    "noVerdictRuns", "spelling"}:
        raise ValueError("queue supports only lanes, carryOver, carryOverChecks, hookCoveredBy, docsOnly, noVerdictRuns "
                         "and spelling")
    if "spelling" in queue and queue["spelling"] is not False:
        raise ValueError("queue.spelling can only be false, which stops the queue running the toolkit's spelling check "
                         "for a project with no spelling lane; true is the default")
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
        passed = {"spelling", "provision", *(named or ["gate"])} & set(lanes)
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
    """Refuse "buildHost", true or false, on any lane or group. It once sent a lane to the second Mac, and
    vocaro-engineering#58 removed it: every heavy or exclusive lane goes there unless it says why it stays,
    so a checkout that still has it is older than its project's change, and reading it as nothing could
    move that checkout's work without a word (guides/shared-host.md, Earlier names)."""
    marked = [name for name, lane in lanes.items() if isinstance(lane, dict) and "buildHost" in lane]
    if not marked: return
    named = ", ".join(marked[:-1]) + " and " + marked[-1] if len(marked) > 1 else marked[0]
    raise ValueError(f"engineering.json gives {named} \"buildHost\", which vocaro-engineering#58 removed: every heavy "
                     "or exclusive lane goes to the second Mac unless it says why it stays, with \"here\": \"REASON\", "
                     f"so delete {'it' if len(marked) == 1 else 'each'}, and rebase a checkout older than that change "
                     "onto main (guides/shared-host.md, Earlier names)")


def place(name, lane):
    """Every admitted lane goes to the second Mac first unless "here" says why it stays on this Mac, such as
    a physical device or a measurement tied to this Mac's hardware, like goldens rendered on its GPU
    (guides/shared-host.md, Which Mac runs a lane)."""
    if "here" not in lane: return
    if not isinstance(lane["here"], str) or not lane["here"].strip():
        raise ValueError(f"{name}.here says why the lane stays on this Mac, such as a physical device or a "
                         "measurement tied to this Mac's hardware: a nonempty reason")
    if unadmitted(lane):
        raise ValueError(f"{name}.here keeps a lane on this Mac, and a self lane runs where it starts already")


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
    (vocaro-engineering#185; guides/shared-host.md, Which Mac runs a lane). A self lane is not placed."""
    if lane.get("unlockedScreen") and unadmitted(lane):
        raise ValueError(f"{name}.unlockedScreen needs an admitted lane: queuekeeper places only an admitted lane on a Mac, "
                         "and a self lane runs where it starts")


def unlocked_screen(lanes, name):
    """Whether the lane name needs an unlocked console screen on the Mac it runs on: a lane, or a group, that says
    "unlockedScreen": true. A group that runs such a lane says so itself, as validation requires."""
    return bool(lanes.get(name, {}).get("unlockedScreen"))


def lane_to_run(config, name):
    """The lane queuekeeper run runs as name, or None when there is no such lane: the one config has, or, for spelling and docs in a
    project with no such lane, the toolkit's check, added to config's lanes so that the run treats it as any other,
    which is how the merge queue's gate runs it (owner's decision, 2026-11-09, vocaro-engineering#305). queue.spelling
    says only whether the queue asks for it, and main's says that, so a batch's own setting cannot leave a lane unknown."""
    lanes = config.setdefault("lanes", {})
    if name == SPELLING and name not in lanes:
        lanes[name] = {"command": list(IMPLICIT_SPELLING), "timeout": SPELLING_LIMIT}
    if name == DOCS and name not in lanes:
        lanes[name] = {"command": list(IMPLICIT_DOCS), "timeout": SPELLING_LIMIT}
    return lanes.get(name)


def stays(lanes, name):
    """Why the lane name runs only on this Mac, as its configuration says, or None for a lane that goes to the
    second Mac first: a self lane runs where it starts, and a lane, or a lane of a group, says why with "here". A
    group runs its lanes under its one admission on one Mac, so a lane of it that stays here keeps the group
    here. A light lane is placed as every lane is (the owner's decision, 2026-11-07). A foreground lane goes to the
    second Mac too, and only there (foreground.py)."""
    lane = lanes[name]
    if unadmitted(lane): return "a self lane runs where it starts, and its script admits each of its parts"
    if lane.get("here"): return f"the lane says: {lane['here']}"
    for member in lane.get("parallel", []):
        if lanes[member].get("here"): return f"its lane {member} says: {lanes[member]['here']}"
    return None


def model(name, lane):
    """A generative-model lane takes one heavy slot and the host's model lock. A self lane is not
    admitted, and exclusive admission holds the model already (guides/shared-host.md)."""
    if lane.get("model") and admission(lane) != "heavy":
        raise ValueError(f"{name}.model needs a heavy lane: a self lane is not admitted, and exclusive "
                         "admission holds the model already")


def whole_host(name, lane):
    """A whole-host lane, "wholeHost": true, is a Mac's work: a gate that runs the project's suites side by
    side. It takes every guaranteed slot of the Mac it runs on, so that a second such lane never runs
    beside it there, and queuekeeper sends it to the Mac that has them all free, while a lighter heavy lane still
    shares (vocaro-engineering#168; guides/shared-host.md, Which Mac runs a lane). A self lane is not
    admitted, and exclusive admission holds the host already."""
    if lane.get("wholeHost") and admission(lane) != "heavy":
        raise ValueError(f"{name}.wholeHost needs a heavy lane: a self lane is not admitted, and exclusive "
                         "admission holds the host already")


def group(name, lane, lanes):
    """A lane group names other lanes to run side by side under its one admission, so it needs
    the heaviest resource among them, the model when one of them runs a model, and one lane at a
    time when several do (guides/parallel-tests.md)."""
    if set(lane) - GROUP_KEYS:
        raise ValueError(f"Lane group {name} takes only parallel, jobs, resource, here, model, wholeHost, "
                         "unlockedScreen and budgetMinutes")
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
    for key in ["model", "wholeHost", "unlockedScreen"]:
        if key in lane and type(lane[key]) is not bool:
            raise ValueError(f"{name}.{key} must be true or false")
    place(name, lane)
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
