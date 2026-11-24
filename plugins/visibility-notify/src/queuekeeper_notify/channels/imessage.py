"""An iMessage to the owner's `to` address, sent through Messages on this Mac. Off until `to` is set.

Every device signed in to that address receives it, test iPhones and iPads among them, so keep those devices off
that account or turn the channel off during device runs. The first message asks macOS to let Queuekeeper control
Messages: allow it once at the Mac, from the process that sends it (`qk notify test --scheduled`)."""
import shutil
import subprocess
import sys

from . import NotDelivered

OSASCRIPT = "/usr/bin/osascript"
LIMIT = 30
# osascript's error when the Automation privacy setting refuses it Messages.
NOT_ALLOWED = "(-1743)"
# The address and text reach the script as arguments, never as source.
SCRIPT = ("on run argv", "tell application \"Messages\"",
          "set chosen to 1st account whose service type = iMessage",
          "send (item 2 of argv) to participant (item 1 of argv) of chosen", "end tell", "end run")


def ready(config):
    if sys.platform != "darwin": return "iMessage is for macOS"
    to = config.get("to")
    if not (isinstance(to, str) and to.strip()):
        return "no address is set: `qk notify set imessage to ADDRESS`"
    return None if shutil.which(OSASCRIPT) else f"{OSASCRIPT} is missing"


def text_of(notice):
    return f"{notice['repository']}: {notice['title']}. {notice['body']}"


def send(notice, config):
    command = [OSASCRIPT, *(part for line in SCRIPT for part in ("-e", line)), config["to"].strip(), text_of(notice)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=LIMIT, stdin=subprocess.DEVNULL)
    except FileNotFoundError as error:
        raise NotDelivered(str(error)) from error
    said = done.stderr.strip() or f"osascript exited {done.returncode}"
    # macOS refusing Queuekeeper control of Messages stops the script before it sends anything.
    if done.returncode and NOT_ALLOWED in said: raise NotDelivered(f"{said}: allow it in System Settings, Privacy "
                                                                   "& Security, Automation")
    if done.returncode: raise RuntimeError(said)
