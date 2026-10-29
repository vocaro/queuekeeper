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
    root = Path(root).resolve()
    value = json.loads((root / "engineering.json").read_text())
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("engineering.json requires schemaVersion 1")
    allowed = {"schemaVersion", "name", "toolkit", "capabilities", "lanes", "exceptions",
               "integration", "hostedRepositories", "voiceover", "repository", "spelling", "simulatorPool",
               "githubArtifacts"}
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
    repositories = value.get("hostedRepositories", [])
    if not isinstance(repositories, list) or any(not isinstance(r, str) or r.count("/") != 1 for r in repositories):
        raise ValueError("hostedRepositories must be owner/repository names")
    voiceover = value.get("voiceover", {})
    if not isinstance(voiceover, dict) or set(voiceover) - {"workflows"}:
        raise ValueError("voiceover supports only an explicit workflow allowlist")
    workflows = voiceover.get("workflows", [])
    if not isinstance(workflows, list) or any(not isinstance(w, str) or not w for w in workflows):
        raise ValueError("VoiceOver workflows must be nonempty names")
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
        if isinstance(lane, dict) and "parallel" in lane:
            group(name, lane, lanes)
            continue
        if not isinstance(lane, dict) or set(lane) - {"command", "resource", "minimumTests", "timeout", "foreground",
                                                       "here", "model", "display"}:
            raise ValueError(f"Invalid lane {name}")
        for key in ["foreground", "model", "display"]:
            if key in lane and type(lane[key]) is not bool:
                raise ValueError(f"{name}.{key} must be true or false")
        if "here" in lane and lane.get("foreground"):
            raise ValueError(f"{name}.here keeps a lane on this Mac, and a foreground lane, which takes the screen, "
                             "runs only here already")
        place(name, lane)
        model(name, lane)
        if lane.get("display") and lane.get("resource", "light") == "light":
            raise ValueError(f"{name}.display needs a heavy or exclusive lane: only an admitted test run holds the "
                             "display's assertions, and a light lane is not admitted")
        command = lane.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f"Lane {name} needs an argv list, never shell source")
        if lane.get("resource", "light") not in {"light", "heavy", "exclusive"}:
            raise ValueError(f"Invalid resource for {name}")
        for key in ["minimumTests", "timeout"]:
            if key in lane and (type(lane[key]) is not int or lane[key] <= 0):
                raise ValueError(f"{name}.{key} must be a positive integer")
    spelling = value.get("spelling", {})
    if not isinstance(spelling, dict) or set(spelling) - {"skip", "exemptPhrases", "exemptRuns", "exemptTokens"}:
        raise ValueError("spelling supports only skip, exemptPhrases, exemptRuns and exemptTokens")
    for key, entries in spelling.items():
        if not isinstance(entries, dict) or any(not isinstance(k, str) or not k.strip() or not isinstance(r, str)
                                                or not r.strip() for k, r in entries.items()):
            raise ValueError(f"spelling.{key} maps each entry to the reason it is kept")
    exceptions = value.get("exceptions", [])
    if not isinstance(exceptions, list) or any(not isinstance(x, dict) or not all(isinstance(x.get(k), str) and x[k] for k in ["rule", "reason"]) for x in exceptions):
        raise ValueError("Exceptions require a rule and a reason")
    return value


RESOURCES = ["light", "heavy", "exclusive"]


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
    """A heavy or exclusive lane goes to the second Mac unless "here" says why it stays on this Mac, such as
    a physical device or a measurement tied to this Mac's hardware, like goldens rendered on its GPU
    (guides/shared-host.md, Which Mac runs a lane)."""
    if "here" not in lane: return
    if not isinstance(lane["here"], str) or not lane["here"].strip():
        raise ValueError(f"{name}.here says why the lane stays on this Mac, such as a physical device or a "
                         "measurement tied to this Mac's hardware: a nonempty reason")
    if lane.get("resource", "light") == "light":
        raise ValueError(f"{name}.here keeps a heavy or exclusive lane on this Mac, and a light lane runs here already")


def stays(lanes, name):
    """Why the lane name runs only on this Mac, as its configuration says, or None for a lane that goes to the
    second Mac: a light lane runs where it starts, a foreground lane takes this Mac's screen, and a lane, or
    a lane of a group, says why with "here". A group runs its lanes under its one admission on one Mac, so a
    lane of it that stays here keeps the group here."""
    lane = lanes[name]
    if lane.get("resource", "light") == "light": return "a light lane runs where it starts"
    if lane.get("foreground"): return "it takes this Mac's screen"
    if lane.get("here"): return f"the lane says: {lane['here']}"
    for member in lane.get("parallel", []):
        if lanes[member].get("here"): return f"its lane {member} says: {lanes[member]['here']}"
    return None


def model(name, lane):
    """A generative-model lane takes one heavy slot and the host's model lock. A light lane is not
    admitted, and exclusive admission holds the model already (guides/shared-host.md)."""
    if lane.get("model") and lane.get("resource", "light") != "heavy":
        raise ValueError(f"{name}.model needs a heavy lane: a light lane is not admitted, and exclusive "
                         "admission holds the model already")


def group(name, lane, lanes):
    """A lane group names other lanes to run side by side under its one admission, so it needs
    the heaviest resource among them, the model when one of them runs a model, and one lane at a
    time when several do (guides/parallel-tests.md)."""
    if set(lane) - {"parallel", "jobs", "resource", "here", "model"}:
        raise ValueError(f"Lane group {name} takes only parallel, jobs, resource, here and model")
    members = lane["parallel"]
    if not isinstance(members, list) or not members or len(set(members)) != len(members) \
            or any(not isinstance(member, str) for member in members):
        raise ValueError(f"{name}.parallel must list distinct lane names")
    resource = lane.get("resource", "light")
    if resource not in RESOURCES:
        raise ValueError(f"Invalid resource for {name}")
    for member in members:
        other = lanes.get(member)
        if not isinstance(other, dict) or "command" not in other:
            raise ValueError(f"{name}.parallel names {member}, which is not a lane with a command")
        if other.get("foreground"):
            raise ValueError(f"{name}.parallel names {member}, which takes the screen and runs alone")
        if RESOURCES.index(other.get("resource", "light")) > RESOURCES.index(resource):
            raise ValueError(f"{name} runs {member} under its own admission, so it needs a resource of at least "
                             f"{other.get('resource')}")
        if other.get("model") and not lane.get("model") and resource != "exclusive":
            raise ValueError(f"{name} runs {member} under its own admission, so it needs \"model\": true or an "
                             "exclusive resource")
    if "jobs" in lane and (type(lane["jobs"]) is not int or not 1 <= lane["jobs"] <= len(members)):
        raise ValueError(f"{name}.jobs must be from 1 to the number of its lanes")
    # One model workload per machine: the group's one model lock cannot keep its own lanes apart.
    models = [member for member in members if lanes[member].get("model")]
    if len(models) > 1 and lane.get("jobs", len(members)) != 1:
        raise ValueError(f"{name} runs the model lanes {', '.join(models)}, which would drive the model at once, "
                         "so it needs \"jobs\": 1")
    if "model" in lane and type(lane["model"]) is not bool:
        raise ValueError(f"{name}.model must be true or false")
    place(name, lane)
    model(name, lane)


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
