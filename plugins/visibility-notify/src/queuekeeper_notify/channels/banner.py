"""A macOS notification banner, posted with osascript. On by itself on a Mac; `sound` names a system sound."""
import shutil
import subprocess
import sys

OSASCRIPT = "/usr/bin/osascript"
# The notice's text reaches the script as arguments, never as source.
SCRIPT = ("on run argv", "if (item 4 of argv) is \"\" then",
          "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)", "else",
          "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv) "
          "sound name (item 4 of argv)", "end if", "end run")


def ready(config):
    if sys.platform != "darwin": return "banners are for macOS"
    return None if shutil.which(OSASCRIPT) else f"{OSASCRIPT} is missing"


def send(notice, config):
    sound = config.get("sound") if isinstance(config.get("sound"), str) else ""
    command = [OSASCRIPT, *(part for line in SCRIPT for part in ("-e", line)),
               notice["title"], notice["body"], notice["repository"], sound]
    done = subprocess.run(command, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    if done.returncode: raise RuntimeError(done.stderr.strip() or f"osascript exited {done.returncode}")
