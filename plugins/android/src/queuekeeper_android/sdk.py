"""Where this Mac's Android SDK and its tools are, and the environment its tools and Gradle run in.

A session's PATH is not the owner's shell's: a run on another Mac starts from ssh, and an agent's shell may lack what
the owner's profile adds. So the SDK is found the way Android Studio and Gradle find it, by ANDROID_HOME, then
ANDROID_SDK_ROOT, then the places its installers use, and each tool is named by its path inside it. A tool that
is missing says which SDK package provides it.
"""
import os
from pathlib import Path
import shutil
import subprocess

# Where the SDK is looked for after the variables, in order: Android Studio's default, then Homebrew's
# android-commandlinetools cask on Apple silicon and on Intel.
DEFAULTS = ("~/Library/Android/sdk", "/opt/homebrew/share/android-commandlinetools",
            "/usr/local/share/android-commandlinetools")
VARIABLES = ("ANDROID_HOME", "ANDROID_SDK_ROOT")
# Each tool: its paths inside the SDK, in order, and the SDK package that provides it.
TOOLS = {
    "adb": (("platform-tools/adb",), "platform-tools"),
    "emulator": (("emulator/emulator",), "emulator"),
    "avdmanager": (("cmdline-tools/latest/bin/avdmanager", "cmdline-tools/bin/avdmanager"), "cmdline-tools;latest"),
    "sdkmanager": (("cmdline-tools/latest/bin/sdkmanager", "cmdline-tools/bin/sdkmanager"), "cmdline-tools;latest"),
}
# The JDK release Gradle's Android plugin and the command-line tools need at least (AGP 9, November 2026).
JAVA_RELEASE = 17
INVENTORY_TIMEOUT = 30


class Missing(RuntimeError):
    """The SDK or one of its tools is not on this Mac: a configuration problem, never a test's failure."""


def root(environ=None):
    """The SDK's folder, or None when this Mac has none."""
    environ = os.environ if environ is None else environ
    for variable in VARIABLES:
        value = environ.get(variable)
        if value and Path(value).expanduser().is_dir(): return Path(value).expanduser()
    for candidate in DEFAULTS:
        path = Path(candidate).expanduser()
        if path.is_dir(): return path
    return None


def tool(name, environ=None):
    """The path of the SDK tool name, or Missing saying how to install it."""
    paths, package = TOOLS[name]
    sdk = root(environ)
    if sdk is not None:
        for relative in paths:
            path = sdk / relative
            if os.access(path, os.X_OK): return str(path)
    found = shutil.which(name)
    if found and not (name == "adb" and sdk is not None): return found
    if sdk is None:
        raise Missing(f"no Android SDK on this Mac: set ANDROID_HOME, or install the command-line tools "
                      f"(`brew install --cask android-commandlinetools`) and then `sdkmanager --install {package!r}`")
    raise Missing(f"{name} is not in the Android SDK at {sdk}: `sdkmanager --install {package!r}` installs it")


def java_home(environ=None):
    """A JDK of JAVA_RELEASE or later: JAVA_HOME when set, else what /usr/libexec/java_home finds; None when there
    is none, since macOS's /usr/bin/java is a stub that only says to install Java."""
    environ = os.environ if environ is None else environ
    if environ.get("JAVA_HOME") and Path(environ["JAVA_HOME"], "bin/java").exists(): return environ["JAVA_HOME"]
    try:
        found = subprocess.run(["/usr/libexec/java_home", "-v", f"{JAVA_RELEASE}+"], capture_output=True, text=True,
                               timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def environment(environ=None):
    """environ with what the SDK's tools and Gradle need: ANDROID_HOME, JAVA_HOME and the tools' folders on PATH."""
    environ = dict(os.environ if environ is None else environ)
    sdk = root(environ)
    if sdk is not None:
        environ.setdefault("ANDROID_HOME", str(sdk))
        folders = [str(sdk / "platform-tools"), str(sdk / "emulator"), str(sdk / "cmdline-tools/latest/bin")]
        environ["PATH"] = os.pathsep.join([*folders, *[part for part in environ.get("PATH", "").split(os.pathsep)
                                                       if part not in folders]])
    java = java_home(environ)
    if java: environ["JAVA_HOME"] = java
    return environ


def package_revisions(output):
    """The platform-tools, emulator and system image revisions from sdkmanager's installed table."""
    packages, installed, table = {}, False, False
    for line in output.splitlines():
        section = line.strip().casefold()
        if section == "installed packages:":
            installed = True
            continue
        if not installed:
            continue
        if section in ("available packages:", "available updates:"):
            break
        columns = [field.strip() for field in line.split("|")]
        if columns[:2] == ["Path", "Version"]:
            table = True
            continue
        if table and (columns[0] in ("platform-tools", "emulator") or columns[0].startswith("system-images;")):
            if len(columns) < 2 or not columns[1]:
                raise ValueError(f"sdkmanager did not report a version for {columns[0]}")
            packages[columns[0]] = columns[1]
    if not table:
        raise ValueError("sdkmanager did not print an installed packages table")
    return packages


def inventory():
    """A read-only SDK package report, or why it could not be read. Runs under either Mac's own login environment."""
    try:
        command = [tool("sdkmanager"), "--list_installed"]
        if installed := root(): command.append(f"--sdk_root={installed}")
        done = subprocess.run(command, env=environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=INVENTORY_TIMEOUT)
        if done.returncode:
            said = (done.stderr.strip() or done.stdout.strip()).splitlines()
            return {"error": f"sdkmanager exited {done.returncode}" + (f": {said[-1][-300:]}" if said else "")}
        return {"packages": package_revisions(done.stdout)}
    except (Missing, OSError, subprocess.SubprocessError, ValueError) as error:
        return {"error": str(error)}
