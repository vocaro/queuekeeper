"""Small, strict project configuration without third-party dependencies."""
import json
import os
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
               "integration", "hostedRepositories", "voiceover", "repository", "spelling", "simulatorPool"}
    if set(value) - allowed:
        raise ValueError(f"Unknown configuration keys: {sorted(set(value) - allowed)}")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("Project name must be a nonempty string")
    toolkit = value.get("toolkit", {})
    if not isinstance(toolkit, dict) or set(toolkit) - {"path"} or not isinstance(toolkit.get("path", DEFAULT_TOOLKIT), str):
        raise ValueError("toolkit must contain only an optional checkout path")
    if value.get("integration", "direct-main") not in {"direct-main", "pull-request"}:
        raise ValueError("Unknown integration policy")
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
    for name, lane in lanes.items():
        if isinstance(lane, dict) and "parallel" in lane:
            group(name, lane, lanes)
            continue
        if not isinstance(lane, dict) or set(lane) - {"command", "resource", "minimumTests", "timeout", "foreground",
                                                       "buildHost", "model"}:
            raise ValueError(f"Invalid lane {name}")
        for key in ["foreground", "buildHost", "model"]:
            if key in lane and type(lane[key]) is not bool:
                raise ValueError(f"{name}.{key} must be true or false")
        if lane.get("buildHost") and (lane.get("foreground") or lane.get("resource", "light") == "light"):
            raise ValueError(f"{name}.buildHost needs a heavy or exclusive lane that does not take the screen")
        model(name, lane)
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
    if set(lane) - {"parallel", "jobs", "resource", "buildHost", "model"}:
        raise ValueError(f"Lane group {name} takes only parallel, jobs, resource, buildHost and model")
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
    for key in ["buildHost", "model"]:
        if key in lane and type(lane[key]) is not bool:
            raise ValueError(f"{name}.{key} must be true or false")
    if lane.get("buildHost") and resource == "light":
        raise ValueError(f"{name}.buildHost needs a heavy or exclusive lane that does not take the screen")
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
