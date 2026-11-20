"""The Android plugin's keys in queuekeeper.json: the project's Gradle build and its emulator pool's name.

    "android": {"directory": "android", "module": "app", "variant": "debug", "gradleArguments": ["--offline"]},
    "emulatorPool": "Pagepour"

directory is the Gradle project's folder in the checkout, the one with gradlew, by default its root; module is the
application module whose app and instrumented tests `qk emulator-tests` builds and runs, by default app; variant is the
build variant, by default debug; gradleArguments go on every Gradle command. emulatorPool names the project's pool,
whose members are its emulators named `POOL LABEL N` (emulators/pool.py).
"""
from pathlib import Path
import re

from queuekeeper import plugins

KEYS = {"directory", "module", "variant", "gradleArguments"}
DEFAULTS = {"directory": ".", "module": "app", "variant": "debug", "gradleArguments": []}


def android(declared):
    if not isinstance(declared, dict) or set(declared) - KEYS:
        raise ValueError(f"android supports only {', '.join(sorted(KEYS))}")
    for key in ("directory", "module", "variant"):
        if key in declared and (not isinstance(declared[key], str) or not declared[key].strip()):
            raise ValueError(f"android.{key} must be a nonempty string")
    directory = declared.get("directory", ".")
    if Path(directory).is_absolute() or ".." in Path(directory).parts:
        raise ValueError("android.directory is a path inside the checkout")
    if "module" in declared and not re.fullmatch(r"[\w.-]+(?::[\w.-]+)*", declared["module"]):
        raise ValueError("android.module is a Gradle project path without its leading colon, such as app or "
                         "feature:reader")
    if "variant" in declared and not re.fullmatch(r"[a-z][A-Za-z0-9]*", declared["variant"]):
        raise ValueError("android.variant is a build variant's name, such as debug or freeDebug")
    arguments = declared.get("gradleArguments", [])
    if not isinstance(arguments, list) or any(not isinstance(word, str) or not word for word in arguments):
        raise ValueError("android.gradleArguments is a list of words, never shell source")


def emulator_pool(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "/" in value:
        raise ValueError("emulatorPool must be a nonempty name, such as the project's")


def configured(config):
    """The project's android settings with the defaults filled in."""
    return {**DEFAULTS, **((config or {}).get("android") or {})}


# The words of a command that reach an emulator or a device: adb, whose server listens on localhost:5037 and starts
# outside any sandbox, the emulator and the commands that run the project's emulator pool. Gradle alone is not one: on
# 2026-11-19, on macOS 27.2, a Gradle build ran in a sandbox given write ["~/.gradle"] and network ["localhost"].
ANDROID_WORDS = frozenset({"adb", "emulator", "emulator-tests", "emulator-pool", "avdmanager"})


def sandbox_refusal(lane):
    """Why the lane cannot run in a sandbox (queuekeeper.sandbox), or None: a lane whose command or compileFirst runs
    adb, the emulator or the emulator pool's commands reaches emulators and devices through adb's server and writes
    ~/.android and the emulators' disks. Such a lane runs unsandboxed. A command is read word by word, so a script
    that runs adb is not caught here."""
    words = {Path(word).name for word in [*lane.get("command", []), *lane.get("compileFirst", [])]
             if isinstance(word, str)}
    found = sorted(words & ANDROID_WORDS)
    if found:
        return (f"its command runs {', '.join(found)}, which reaches emulators and devices through adb's server and "
                "writes ~/.android")
    return None


def register():
    plugins.CONFIG_KEYS.update(android=android, emulatorPool=emulator_pool)
    if sandbox_refusal not in plugins.SANDBOX_REFUSALS: plugins.SANDBOX_REFUSALS.append(sandbox_refusal)
