"""A direct notice to the owner: a banner on this Mac, posted with osascript, for what Queuekeeper did by itself that
he should know of even when the notify feature is off, such as putting back his device's notifications
(queuekeeper_mobile.upkeep, vocaro/queuekeeper#174). The banner stays in Notification Center until he reads it."""
import os
import subprocess
import sys

OSASCRIPT = "/usr/bin/osascript"
# "off": no banner is posted; tests set it for every process they start (host_isolation).
SWITCH = "QUEUEKEEPER_OWNER_NOTICE"
# The notice's text reaches the script as arguments, never as source.
SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv) subtitle \"Queuekeeper\"",
          "end run")


def execute(arguments):
    """Run arguments; (exit status, what they said), or (None, why) when they did not finish."""
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    return done.returncode, (done.stderr or "").strip()


# What posts the banner, alone, so that tests replace it.
run = execute


def tell(title, body):
    """Post title and body as a banner on this Mac; what became of it, in words a record keeps."""
    if os.environ.get(SWITCH) == "off": return f"no banner: {SWITCH} is off"
    if sys.platform != "darwin": return "no banner: banners are for macOS"
    status, said = run([OSASCRIPT, *(part for line in SCRIPT for part in ("-e", line)), title, body])
    if status == 0: return "a banner on this Mac"
    return f"no banner: osascript {'did not finish' if status is None else f'exited {status}'}: {said or 'no output'}"
