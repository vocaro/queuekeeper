"""What the Apple plugin does around a lane's run and an admitted command's: the app registrations it keeps and
the VoiceOver state it puts back. The device windows a run waits in and the device pass it reads are the mobile
plugin's (queuekeeper_mobile.lane), which asks this one's platform about an iPhone or iPad (apple_platform). The
foreground a run holds is the Mac desktop plugin's (queuekeeper_mac_desktop.lane)."""
import contextlib

from queuekeeper import plugins
from queuekeeper_mobile import platforms
from . import app_builds, app_registrations, apple_platform, run_results, xctest_shards
from queuekeeper_mac_desktop import foreground, reservations
from .simulators import matrix, pool as simulator_pool
from .voiceover import state as voiceover_state


@contextlib.contextmanager
def preserved_voiceover(owner, *, record, said, voiceover=None):
    """The Mac's VoiceOver state, kept for a run that holds its foreground (mac_desktop.foreground.GUARDS): read once
    the foreground is held, by voiceover when given, and put back before it is released. Always settled: the run's
    child driver may have died between a failed toggle and its own restoration, and nothing tells this process so.
    The foreground is released only once the original state has held (#137). The original is recorded in the entry
    as voiceOverOriginal, with the lease it was read under, for the run's own runner to restore over a session's
    unverified restoration (voiceover_state.recover_restoration; vocaro/EPUBLib#416). A passive driver opts into
    voiceover.state.preserved(target=False) inside the lease itself."""
    with voiceover_state.preserved(owner, record=record, read_state=voiceover, said=said, settle=True) as original:
        current = record.entry(reservations.FOREGROUND)
        reserved, pid, start = reservations.identity(current)
        record.update(reservations.FOREGROUND, owner=owner, revision=current["revision"], voiceOverOriginal={
            "enabled": original, "reservedAt": reserved, "hostProcessIdentifier": pid, "hostProcessStart": start})
        yield


def register():
    """Put the plugin's lane hooks and commands in the core."""
    platforms.register(apple_platform.PLATFORM)
    if preserved_voiceover not in foreground.GUARDS: foreground.GUARDS.append(preserved_voiceover)
    for hooks, hook in ((plugins.AROUND_RUN, app_registrations.around_run), (plugins.REPORT_RESULTS, run_results.report),
                        (plugins.TEST_ALLOWANCE, matrix.default_test_allowance)):
        if hook not in hooks: hooks.append(hook)
    # Each runs tests itself, so one started outside an admission is admitted first (cli.bare_runner).
    plugins.RUNNERS.update({("simulator-tests",), ("xctest",)})
    plugins.COMMANDS.update({
        "simulator-tests": ("build a scheme's tests once and run them on several members of the project's simulator "
                            "pool side by side: qk simulator-tests --scheme S --runtime R --device T --device T; "
                            "-h for the rest", matrix.main),
        "simulator-pool": ("list, take, release, prune, reap or check members of the project's simulator pool under "
                           "device-record reservations: qk simulator-pool list|take|release|prune|reap|check; "
                           "-h for the rest", simulator_pool.main),
        "results": ("keep a test run's per-test outcomes and seconds in the lane's results/ and print them, for a "
                    "script that runs xcodebuild itself: qk results keep; -h for the rest",
                    lambda arguments, project: run_results.main(arguments)),
        "xctest": ("build a Swift package's tests once and run its XCTest classes in xctest processes of their own, "
                   "side by side, each reporting its skips: qk xctest [-j JOBS] [--filter REGEX]; "
                   "qk xctest -h for the rest", lambda arguments, project: xctest_shards.main(arguments)),
    })
    plugins.PARSED_COMMANDS.update({
        "app": ("run the last good build of the project's app: qk app run opens the newest kept Mac build, and "
                "qk app run --iphone or --ipad installs and launches the device build on that paired device, "
                "building origin/main first when the kept build is older; qk app keep builds and keeps one, as "
                "the merge queue does after each gated landing; qk app status lists them; qk app build builds "
                "this checkout as a probe build, which qk app run --app PATH (or --here) runs",
                app_builds.add_arguments, app_builds.execute, True),
    })
    if keep_app not in plugins.AFTER_LANDING: plugins.AFTER_LANDING.append(keep_app)


def keep_app(checkout, config, head, log, environment):
    """Keep the project's app at head once a gated batch has landed, when its configuration asks for one
    (app_builds.py): the build is started and left to run. Returns whether one was started."""
    if not app_builds.after_landing(config): return None
    return app_builds.spawn_keep(checkout, head, log, env=environment)
