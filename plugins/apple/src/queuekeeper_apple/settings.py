"""Configuration and command interpretation owned by the Apple plugin."""
from pathlib import Path
import re
from queuekeeper import config, extensions

APP_PLATFORMS = {"mac": True, "ios": False}

def app(declared):
    """The app `qk app` keeps and runs (app_builds.py, guides/app-builds.md)."""
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


def device_keys(name, lane):
    """A lane's "devices", the entries of the device record its run uses, which a device pass's own --device options
    say without it. Only an admitted lane waits for admission, which they bound (lane_devices)."""
    if "devices" not in lane: return
    keys = lane["devices"]
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError(f"{name}.devices must be a non-empty list of device entries, such as \"iPhone\"")
    if config.unadmitted(lane):
        raise ValueError(f"{name}.devices needs an admitted lane: it bounds the wait for admission, and a self lane "
                         "is not admitted")


def lane_devices(lane):
    """The device-record entries a lane's run uses (vocaro-engineering#292): its "devices", for a project's script
    that calls device_pass.run, or each --device of an `qk device-pass` command, up to that command's own --, which
    the words of the command it runs follow. Any other lane uses none."""
    if lane.get("devices"): return list(lane["devices"])
    command = lane.get("command") or []
    if "device-pass" not in command: return []
    words = command[command.index("device-pass") + 1:]
    if "--" in words: words = words[:words.index("--")]
    return [word.split("=", 1)[1] if word.startswith("--device=") else words[index + 1]
            for index, word in enumerate(words) if word.startswith("--device=")
            or (word == "--device" and index + 1 < len(words))]


class DeviceKeys(extensions.LaneKey):
    def check(self, name, lane):
        device_keys(name, lane)


class DeviceMinutes(extensions.LaneKey):
    def check(self, name, lane):
        minutes = lane["deviceRunMinutes"]
        if type(minutes) is not int or minutes <= 0:
            raise ValueError(f"{name}.deviceRunMinutes must be a positive integer")
        if not lane_devices(lane) or config.unadmitted(lane):
            raise ValueError(f"{name}.deviceRunMinutes needs an admitted device lane")


def simulator_pool(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("simulatorPool must be a nonempty name, such as the project's")


def register():
    extensions.CONFIG_KEYS.update(app=app, simulatorPool=simulator_pool)
    extensions.LANE_KEYS.update(devices=DeviceKeys(), deviceRunMinutes=DeviceMinutes())
