"""A Mac's chip, as a lane names it: "Apple M5 Max" is apple-m5-max. On Apple silicon the chip is the CPU and the
GPU both, so it is what a result that differs by GPU differs by."""
import re
import subprocess

from .. import remote

READ = ["sysctl", "-n", "machdep.cpu.brand_string"]
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# The second Mac's chip by its SSH name, read once a process: a Mac's chip does not change.
THERE = {}


def slug(name):
    """A chip's name as a lane writes it: lowercase, its words joined by hyphens."""
    return "-".join(re.findall(r"[a-z0-9]+", name.lower()))


def here():
    """This Mac's chip, or None when it cannot be read."""
    try: ran = subprocess.run(READ, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError): return None
    return slug(ran.stdout) or None if ran.returncode == 0 else None


def second_mac():
    """The second Mac this Mac sends work to, or None."""
    try: return remote.configured()
    except ValueError: return None


def there(host):
    """The chip of the second Mac host, or None when it could not be asked."""
    if host not in THERE:
        try: ran = remote.ssh(host, " ".join(READ), capture_output=True, text=True, timeout=30)
        except Exception: return None  # noqa: BLE001 -- a Mac that cannot be reached has an unread chip
        if ran.returncode != 0 or not slug(ran.stdout): return None
        THERE[host] = slug(ran.stdout.strip().splitlines()[-1])
    return THERE[host]
