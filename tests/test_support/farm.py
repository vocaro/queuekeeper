"""A farm for a test: the Macs its farm.json names, in the test's HEAVY_SLOT_DIR namespace (queuekeeper.farm)."""
import contextlib
import json
import uuid
from unittest.mock import patch

from queuekeeper import farm, remote


def mac(host, name=None, state=farm.ACTIVE):
    """A farm Mac reached at the SSH destination host, named name, else host."""
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, host)).upper(), "name": name or host, "ssh": host,
            "state": state, "added": "2026-11-15T00:00:00Z"}


def write(*hosts):
    """Make the farm the Macs reached at hosts, in that order: each an SSH destination, or a mac()."""
    path = farm.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    macs = [host if isinstance(host, dict) else mac(host) for host in hosts]
    path.write_text(json.dumps({"version": farm.VERSION, "macs": macs}))
    return path


@contextlib.contextmanager
def only(*hosts):
    """The farm as write(*hosts) makes it, for the block, and as it was after it."""
    path = farm.path()
    before = path.read_bytes() if path.exists() else None
    write(*hosts)
    try:
        yield path
    finally:
        if before is None: path.unlink(missing_ok=True)
        else: path.write_bytes(before)


def use(case, *hosts):
    """Make the farm write(*hosts)'s for the rest of the test case's test, in whatever namespace it runs in."""
    entered = only(*hosts)
    entered.__enter__()
    case.addCleanup(entered.__exit__, None, None, None)


def choice(offered, *hosts):
    """Why an offer this Mac may take goes to this Mac's queue rather than to the Macs at hosts, else to the one
    asked last, all taken as reached; or None when one of them is offered it (remote.Offer.attempt)."""
    offered.hosts, offered.choosing = hosts or ((offered.host or "fake"),), True
    with patch.object(remote, "reachable"):
        try: offered.attempt()
        except remote.Loaded as loaded: return str(loaded)
    return None
