"""What `queuekeeper doctor` checks of a Unity project: that its Unity project is where it says, that the editor it
pins is installed on this Mac and on the second Mac, and that no lane runs the editor without a graphics device.

Whether Unity Hub is signed in is not checked: nothing on disk was found to say so (this Mac, Unity 6000.6.0f1,
2026-11-12). A run on a Mac that is not signed in ends unmeasured, by its log (licensing.never_started)."""
import shlex

from .. import extensions, remote
from . import editor, settings

# Without a graphics device Unity never loads compute shaders, and a project that has them fails to open.
NO_GRAPHICS = "-nographics"


def second_mac():
    """The second Mac this Mac sends work to, or None."""
    try: return remote.configured()
    except ValueError: return None


def installed_there(host, binary):
    """Whether host has the editor's binary: True, False, or None when it could not be asked."""
    try: ran = remote.ssh(host, f"test -x {shlex.quote(str(binary))}", capture_output=True, text=True, timeout=30)
    except Exception: return None  # noqa: BLE001 -- a Mac that cannot be reached is doctor's own finding
    return {0: True, 1: False}.get(ran.returncode)


def warnings(root, config):
    found = []
    project = settings.project(root, config)
    for name, lane in sorted(config.get("lanes", {}).items()):
        if isinstance(lane, dict) and NO_GRAPHICS in [str(word).lower() for word in lane.get("command") or []]:
            found.append(f"{name} passes {NO_GRAPHICS}: without a graphics device Unity never loads compute shaders, "
                         "and a project that has them fails to open")
    if not (project / "ProjectSettings").is_dir():
        return found + [f"the Unity extension is on, but there is no Unity project at {project}: name its folder, as "
                        f"\"{settings.KEY}\": {{\"project\": \"lab\"}}"]
    want = editor.pinned(project)
    if not want:
        return found + [f"{project} pins no editor version (ProjectSettings/ProjectVersion.txt), so a run takes this "
                        "Mac's newest"]
    pinned = editor.HUB / want / editor.BINARY
    if editor.resolve(editor.HUB, want) != pinned:
        found.append(f"Unity {want}, which {project.name} pins, is not installed on this Mac ({editor.HUB / want}): a "
                     "run here takes the newest editor instead, which reimports the Library and rewrites the pin")
    host = second_mac()
    if host and installed_there(host, pinned) is False:
        found.append(f"Unity {want}, which {project.name} pins, is not installed on {host} ({editor.HUB / want}): a "
                     "run sent there takes its newest editor, or none")
    return found


def register():
    """Add these checks to `doctor`."""
    if warnings not in extensions.DOCTOR: extensions.DOCTOR.append(warnings)
