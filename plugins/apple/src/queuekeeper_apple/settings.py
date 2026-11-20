"""Configuration and command interpretation owned by the Apple plugin. A lane's devices are the mobile plugin's
(queuekeeper_mobile.settings)."""
from pathlib import Path
import re
from queuekeeper import config, plugins

APP_PLATFORMS = {"mac": True, "ios": False}

def app(declared):
    """The app `qk app` keeps and runs (app_builds.py)."""
    if not isinstance(declared, dict) or set(declared) - {"project", "workspace", "directory", "configuration",
                                                          "prepare", "links", *APP_PLATFORMS}:
        raise ValueError("app supports only project or workspace, directory, configuration, prepare, links, mac and ios")
    links = declared.get("links", [])
    if not isinstance(links, list) or any(not isinstance(link, str) or not link.strip() for link in links):
        raise ValueError("app.links lists the checkouts beside the project that its build names by a relative path")
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


class Idioms(plugins.LaneKey):
    """A lane's "idioms": why it runs its whole selection on an iPhone and an iPad simulator, which `qk doctor`
    warns of otherwise (doctor.idiom_warnings). It changes nothing in the lane's run."""
    def check(self, name, lane):
        if not isinstance(lane["idioms"], str) or not lane["idioms"].strip():
            raise ValueError(f"{name}.idioms must say why the lane's whole selection depends on the idiom")


def simulator_pool(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("simulatorPool must be a nonempty name, such as the project's")


# The words that start a command that runs Xcode or SwiftPM, which a lane's sandbox cannot hold (sandbox_refusal).
XCODE_WORDS = frozenset({"xcodebuild", "xcrun", "swift", "simulator-tests", "xctest", "xcodes", "simctl", "devicectl"})


def sandbox_refusal(lane):
    """Why the lane cannot run in a sandbox (queuekeeper.sandbox), or None. xcodebuild applies a sandbox of its own to
    a target's script phases (ENABLE_USER_SCRIPT_SANDBOXING) and SwiftPM to its manifests and plugins, and macOS refuses
    one inside another (sandbox_apply: Operation not permitted, measured on macOS 27.2); Xcode, its simulators and
    CoreDevice also write to ~/Library/Developer and reach devices over the network. Such a lane runs unsandboxed.
    A lane's command is read word by word, so a script that runs xcodebuild is not caught here: it fails as it starts
    it, and its run's summary names the tool (nested_sandbox)."""
    if lane.get("idioms") is not None: return "a lane with idioms runs on simulators, which Xcode runs"
    words = {Path(word).name for word in [*lane.get("command", []), *lane.get("compileFirst", [])] if isinstance(word, str)}
    found = sorted(words & XCODE_WORDS)
    if found:
        return (f"its command runs {', '.join(found)}, which applies a sandbox of its own, and macOS refuses one inside "
                "another")
    return None


# What a log says of the Apple tool whose own sandbox was refused inside a lane's (nested_sandbox): SwiftPM's manifest
# and plugin steps, measured on macOS 27.2 as "Invalid manifest" before "sandbox_apply", and xcodebuild's script phases.
NESTED_TOOLS = (
    ("SwiftPM, for a package's manifest or plugins",
     re.compile(r"Invalid manifest|PackageDescription|swift-(?:build|test|package)|swiftpm", re.I)),
    ("xcodebuild, for a script phase (ENABLE_USER_SCRIPT_SANDBOXING)",
     re.compile(r"PhaseScriptExecution|xcodebuild")))


def nested_sandbox(text):
    """The Apple tool a sandboxed lane's log, text, shows applied a sandbox of its own, which macOS refused inside the
    lane's (queuekeeper.sandbox.observed), or None: how a script that runs xcodebuild or SwiftPM, which sandbox_refusal
    cannot read, is told once it fails."""
    return next((tool for tool, said in NESTED_TOOLS if said.search(text)), None)


def register():
    plugins.CONFIG_KEYS.update(app=app, simulatorPool=simulator_pool)
    plugins.LANE_KEYS.update(idioms=Idioms())
    if sandbox_refusal not in plugins.SANDBOX_REFUSALS: plugins.SANDBOX_REFUSALS.append(sandbox_refusal)
    if nested_sandbox not in plugins.SANDBOX_NESTED: plugins.SANDBOX_NESTED.append(nested_sandbox)
