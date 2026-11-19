"""What `qk doctor` checks of a Unity project: that its Unity project is where it says, that the editor it
pins is installed on this Mac and on another Mac, that Unity Hub is signed in on this Mac, and that no lane runs
the editor without a graphics device.

The sign-in is read with Unity's own command (unity_cli.signed_in), on this Mac only: over SSH the command may not
read the keychain that holds the session, so another Mac's is not asked. A sign-in that cannot be read is not
warned of. A run on a Mac that is not signed in ends unmeasured, by its log (licensing.never_started)."""
import shlex

from queuekeeper import plugins, farm, remote
from . import editor, settings, unity_cli

# Without a graphics device Unity never loads compute shaders, and a project that has them fails to open.
NO_GRAPHICS = "-nographics"


def farm_macs():
    """The SSH destinations of the farm's Macs this Mac sends work to now."""
    try: return farm.destinations()
    except ValueError: return ()


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
        return found + [f"the Unity plugin is on, but there is no Unity project at {project}: name its folder, as "
                        f"\"{settings.KEY}\": {{\"project\": \"lab\"}}"]
    want = editor.pinned(project)
    if not want:
        return found + [f"{project} pins no editor version (ProjectSettings/ProjectVersion.txt), so a run takes this "
                        "Mac's newest"]
    pinned = editor.HUB / want / editor.BINARY
    if editor.resolve(editor.HUB, want) != pinned:
        found.append(f"Unity {want}, which {project.name} pins, is not installed on this Mac ({editor.HUB / want}): a "
                     "run here takes the newest editor instead, which reimports the Library and rewrites the pin")
    if unity_cli.signed_in() is False:
        found.append("Unity Hub is not signed in on this Mac, so an editor started here has no license and its run "
                     "ends unmeasured: sign in with Unity Hub, or `unity auth login`")
    for host in farm_macs():
        if installed_there(host, pinned) is not False: continue
        found.append(f"Unity {want}, which {project.name} pins, is not installed on {host} ({editor.HUB / want}): a "
                     "run sent there takes its newest editor, or none")
    return found


def register():
    """Add these checks to `doctor`."""
    if warnings not in plugins.DOCTOR: plugins.DOCTOR.append(warnings)
