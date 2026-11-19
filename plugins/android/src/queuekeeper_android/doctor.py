"""What the Android plugin adds to `qk doctor`: whether this Mac has the SDK, its tools and a JDK that the project's
configuration needs."""
from queuekeeper import plugins
from . import sdk


def warnings(root, config):
    """A warning for each missing piece a project that builds an Android app or keeps an emulator pool needs."""
    if not (config.get("android") or config.get("emulatorPool")): return []
    found = []
    for name in ("adb", "emulator", "avdmanager"):
        try: sdk.tool(name)
        except sdk.Missing as error: found.append(f"android: {error}")
    if sdk.java_home() is None:
        found.append(f"android: no JDK {sdk.JAVA_RELEASE} or later, which Gradle and avdmanager need: "
                     "`brew install --cask temurin@21`, or set JAVA_HOME")
    return found


def register():
    if warnings not in plugins.DOCTOR: plugins.DOCTOR.append(warnings)
