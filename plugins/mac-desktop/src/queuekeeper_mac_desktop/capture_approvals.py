"""macOS's direct screen-capture approvals: launch a capturing runner from one path, and see when they lapse.

The dialog "… is requesting to bypass the system private window picker and directly access your screen and
audio" is separate from the Screen Recording list and CGPreflightScreenCaptureAccess. macOS records each Allow in
replayd's ScreenCaptureApprovals.plist, keyed by the app's file URL (or a bundle ID or executable path for some),
and asks again for any other path, and again for the same path once its hint date passes, 30 days after its last
prompt. Signature and CDHash do not enter into it (second Mac, macOS 27.2 26B123, 2026-11-06; vocaro-engineering#209,
doc/guides/foreground.md, Screen recording for commands started over SSH).

So a test runner that captures through ScreenCaptureKit launches from one path per host, outside the checkout slots
(stage and rebind below), and `qk remote foreground` lists when that path's approval, and the SSH command
host's, lapse. Nothing here writes the approvals: editing that plist is not a supported mechanism.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

# Another Mac reads them with its system Python, before the toolkit is checked out there.
from queuekeeper.offload_host import CAPTURE_RUNNERS as RUNNERS, screen_capture_approvals as lapses


def stable_runner_path(project, bundle, home=None):
    """The one path a project's capturing runner bundle launches from on this host."""
    return Path(home if home is not None else Path.home()) / RUNNERS / project / bundle


def rebind(value, built, stable):
    """value, an xctestrun's contents, with every path at or inside the built bundle moved to the stable one."""
    built, stable = str(built), str(stable)
    if isinstance(value, dict):
        return {key: rebind(item, built, stable) for key, item in value.items()}
    if isinstance(value, list):
        return [rebind(item, built, stable) for item in value]
    if isinstance(value, str) and (value == built or value.startswith(built + "/")):
        return stable + value[len(built):]
    return value


def stage(built, stable, running):
    """Replace the stable copy with the built bundle, with ditto, which keeps its signature; return stable.

    running lists the processes that run from stable, which the caller observes; stage refuses while there is
    one. The caller holds the host's foreground lease, which no other foreground run holds at the same time.
    """
    built, stable = Path(built), Path(stable)
    if not (built / "Contents/Info.plist").is_file():
        raise RuntimeError(f"built runner missing: {built}")
    if running:
        raise RuntimeError(f"a runner is still running from {stable}; not replacing it")
    stable.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = stable.with_name(stable.name + f".staging-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    subprocess.run(["ditto", str(built), str(staging)], check=True, timeout=300)
    retired = stable.with_name(stable.name + f".retired-{os.getpid()}")
    if stable.exists():
        os.rename(stable, retired)
    os.rename(staging, stable)
    shutil.rmtree(retired, ignore_errors=True)
    return stable


def report(found, now=None):
    """What `qk remote foreground` says of these approvals: a line for each, lapsed or not. They are the owner's
    to renew, at the prompt, about once a month (owner's decision, 2026-11-06), and never stop admission."""
    if found is None:
        return ["- its direct screen-capture approvals cannot be read (replayd's ScreenCaptureApprovals.plist)"]
    now = now or datetime.now(timezone.utc)
    lines = []
    for item in found:
        lapse = datetime.fromisoformat(item["lapses"])
        state = "lapsed" if lapse <= now else "lapses"
        lines.append(f"- direct screen-capture approval for {item['app']} {state} {lapse:%Y-%m-%d %H:%M} UTC; the "
                     "first capture after that raises the \"bypass the system private window picker\" prompt, which the "
                     "owner allows, and stops that run unmeasured")
    return lines
