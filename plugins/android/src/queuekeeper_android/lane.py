"""The Android plugin's commands and its platform: `qk emulator-tests` and `qk emulator-pool`, and the Android
phones and tablets the shared device record holds (queuekeeper_mobile; android_platform)."""
from queuekeeper import plugins
from queuekeeper_mobile import platforms
from . import adb, android_platform, emulator_tests
from .emulators import pool as emulator_pool


def prepare(root, config):
    """Before an admitted lane or command of a project that uses Android starts: this Mac's adb server, started from
    queuekeeper's own process, so that the lane's command, which ends with its supervision, does not own it."""
    if config and (config.get("android") or config.get("emulatorPool")): adb.start_server()


def register():
    platforms.register(android_platform.PLATFORM)
    if prepare not in plugins.PREPARE_LANE: plugins.PREPARE_LANE.append(prepare)
    if emulator_tests.report not in plugins.REPORT_RESULTS: plugins.REPORT_RESULTS.append(emulator_tests.report)
    # It runs tests itself, so one started outside an admission is admitted first (cli.bare_runner).
    plugins.RUNNERS.add(("emulator-tests",))
    plugins.COMMANDS.update({
        "emulator-tests": ("build an Android app's instrumented tests once and run them on several members of the "
                           "project's emulator pool side by side: qk emulator-tests --device pixel_8 --device "
                           "pixel_tablet; -h for the rest", emulator_tests.main),
        "emulator-pool": ("list, take, release or warm members of the project's Android emulator pool under "
                          "device-record reservations: qk emulator-pool list|take|release|release-mine|template; "
                          "-h for the rest", emulator_pool.main),
    })
