"""A lane's "display": true, which keeps a Mac's display awake for the lane's run.

A lane that says it waits for what the display's refresh times, such as a scroll view's animation. Its command
runs under a one-second caffeinate -u, which wakes a sleeping display, behind a lock too, and then caffeinate -d,
which keeps an awake one on but wakes none, and no other assertion (apple-engineering's guides/background-mac-testing.md, Lock screen
and display sleep). It unlocks nothing: a lane whose tests need an unlocked screen says "unlockedScreen" too."""
from .. import extensions
from ..config import unadmitted

KEY = "display"
AWAKE = ["sh", "-c", 'caffeinate -u -t 1; exec caffeinate -d -- "$@"', "sh"]


class Display(extensions.LaneKey):
    # Such a lane's result depends on the screen, which is read on the Mac that runs it (vocaro-engineering#185).
    reads_screen = True

    def check(self, name, lane):
        if type(lane[KEY]) is not bool:
            raise ValueError(f"{name}.{KEY} must be true or false")
        if lane[KEY] and unadmitted(lane):
            raise ValueError(f"{name}.{KEY} needs an admitted lane: only an admitted test run holds the display's "
                             "assertions, and a self lane is not admitted")

    def command(self, lane, command):
        """command under AWAKE when the lane says "display": true, where the receipt's command shows the
        assertions. caffeinate -d execs the command in its own process and holds the assertion from a child of
        it, which ends when the command does, so the command keeps the run's PID and status and the assertion
        ends with it, however it ends (this Mac, macOS 27.0.1, 2026-10-28)."""
        return [*AWAKE, *command] if lane.get(KEY) else command


def warnings(root, config):
    """qk doctor's warning for each "display": true lane that does not say "unlockedScreen": "display" wakes the
    display and keeps it on, behind a lock too, but unlocks nothing, so queuekeeper may place the lane on a Mac whose screen
    is locked, where a test that reports a lock as unmeasured gives no verdict, and the merge queue sends its gate
    there again until it holds (vocaro-engineering#293). A lane whose tests measure behind a lock says
    "unlockedScreen": false, which settles the question; only a lane that leaves it out is warned of."""
    lanes = config.get("lanes", {})
    found = []
    for name, lane in sorted(lanes.items()):
        if not isinstance(lane, dict) or not lane.get(KEY) or "unlockedScreen" in lane: continue
        groups = sorted(group for group, other in lanes.items() if isinstance(other, dict)
                        and name in (other.get("parallel") or []) and not other.get("unlockedScreen"))
        also = f", and so does each group that runs it, {', '.join(groups)}," if groups else ""
        found.append(f"{name} says \"display\": true, which wakes the display but does not unlock the screen, and "
                     "does not say \"unlockedScreen\", so queuekeeper may place it on a Mac whose screen is locked, where a "
                     "test that reports a lock as unmeasured gives no verdict: if its tests need an unlocked "
                     f"screen, it needs \"unlockedScreen\": true{also} and if they measure behind a lock, say "
                     "\"unlockedScreen\": false (guides/shared-host.md, Which Mac runs a lane)")
    return found


def register():
    """Add the key and its doctor check to the core."""
    if not isinstance(extensions.LANE_KEYS.get(KEY), Display): extensions.LANE_KEYS[KEY] = Display()
    if warnings not in extensions.DOCTOR: extensions.DOCTOR.append(warnings)
