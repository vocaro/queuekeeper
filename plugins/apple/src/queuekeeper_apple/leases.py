"""The physical devices a run on another Mac uses, and what else a dispatch asks of the Apple extension's devices:
the hardware a command names and the project's simulator pool. The core's remote calls these through
queuekeeper.extensions.DEVICES, which lane.py's Devices, built on the one here, fills."""
from pathlib import Path
import sys

from queuekeeper import extensions
from . import devices
from .simulators import pool as simulator_pool


class Lease(devices.BoundedRun):
    """Physical devices a run on another Mac uses, each reserved in this Mac's record by the session
    that sends the run (doc/guides/shared-host.md, Physical devices and another Mac): a bounded run
    whose command runs there.

    The dispatch marks each entry's run active with its own process, whose end, by a stop, its death
    or a dropped connection, ends the run there: a run whose dispatcher vanished is an orphan here.
    The connection's ssh is recorded as the entry's own process, which release and recovery wait
    for. Once the bound passes, the dispatch stops the run there as a stop does. It records the
    run's outcome when it ends; releasing the device stays the session's. So such a run keeps no journal
    there (offload_host.Journal): unlike any other, it does not go on after a dropped connection.

    Its command is the dispatch, so that devices.watched_run watches each leased device's log from
    this Mac while the dispatch lasts and sends it once more when a banner was presented (#192): the
    relay runs here, the Mac whose record holds the reservation, silences and restores the device,
    so the owner's lines never leave it, and offload_host.py, which imports nothing of the toolkit,
    runs no copy of the watch."""

    def __init__(self, keys, owner, words, *, record, root, budget, commits=()):
        super().__init__(keys, owner, words, record=record, root=root, commits=commits)
        self.window = {"devices": dict(self.devices), "until": self.deadline.isoformat(),
                       "cleanupSeconds": self.keep, "budgetSeconds": budget}

    def execute(self, send, *, run=None, watch=None, **_unused):
        """send()'s status, the dispatch, with the run it makes recorded in each leased entry here, and what
        watch, a watch of the devices' logs (device_notifications.Watch), saw during it as its
        notificationWatch."""
        self.timed_out = False
        try:
            self.begin({**(run or {}), "startedAt": devices.now()})
        except BaseException:
            if watch is not None: watch.stop()
            raise
        outcome = None
        try:
            try:
                devices.report_window(self.window)
                status = send()
            finally:
                if watch is not None: self.run = {**self.run, "notificationWatch": watch.stop()}
            outcome = f"exit {status}"
            return status
        except BaseException as error:
            outcome = f"{type(error).__name__}: {error}"
            raise
        finally:
            if self.timed_out: outcome = f"stopped at its {self.run['boundSeconds']}s bound, then {outcome}"
            self.finish(outcome)

    def plan(self, mac):
        return {"record": mac, **self.window}

    def connected(self, pid):
        try: self.own(pid)
        except (devices.Refused, OSError, TimeoutError) as error:
            print(f"remote: the device entries did not record the connection's process {pid}: {error}",
                  file=sys.stderr, flush=True)


# What a refusal adds for forty hex digits, which may be a commit the checkout's Git lacks rather than an older
# device's UDID (#91). --git-commit exempts only the hash as an argument of its own (remote.commit_arguments).
FOREIGN_COMMIT = (" If it is instead a commit of another repository, which this checkout's Git lacks, declare it "
                  "with `qk remote admit --git-commit REPOSITORY=SHA` and pass the hash as a separate argument: "
                  "the option never exempts a hash inside a shell string such as bash -c's.")


def named_devices(words, record=None):
    """{KEY: UDID} of the physical devices in this Mac's record that words name."""
    named = " ".join(words).upper()
    if not devices.PHYSICAL.search(named): return {}
    entries = (record or devices.Record()).load()["devices"]
    return {key: udid for key, entry in entries.items() for udid in [str(entry.get("device") or "").upper()]
            if devices.physical(udid) and udid in named}


def lease(keys, owner, words, record=None, root=None, *, budget=None, commits=()):
    """The reservations held here that a run on another Mac carries, or None when it uses no physical
    device.

    The keys are a bounded run's (devices.BoundedRun): owner holds each one's physical device here
    with no run active, within the window the owner agreed, and every device the command names is one of
    them. Another Mac reaches the paired devices too, and its own record refuses them, so
    without a key a command that names any hardware UDID is refused, whether or not this record
    holds its device: a run on a device goes through a reservation. Forty hex digits that the Git
    of root's checkout knows as an object, such as a commit's whole hash, name no device (devices.named).
    commits contains only hashes verified by commit_arguments; it exempts exact positional arguments."""
    record = record or devices.Record()
    if not keys:
        named = named_devices(words, record)
        if named:
            key, udid = next(iter(named.items()))
            raise devices.Refused(f"the command names {key}'s device {udid}, which no --device covers: reserve {key} "
                                  f"here and add --device {key}. Nothing was sent")
        # A device no entry here holds, which another Mac may reach all the same.
        for udid in sorted(devices.named(words, root=root, commits=commits)):
            raise devices.Refused(f"the command names the hardware UDID {udid}, which no entry of this Mac's record "
                                  "holds and no --device covers: reserve its device here under a key and add "
                                  f"--device KEY.{FOREIGN_COMMIT if len(udid) == 40 else ''} Nothing was sent")
        return None
    if not owner:
        raise ValueError(f"name the session whose reservations --device carries: --device-owner, or {devices.OWNER}")
    return Lease(keys, owner, words, record=record, root=root, budget=budget, commits=commits)


def carrying(lease, root, host, work, name, send):
    """send()'s status, with the run it makes recorded in each leased device's entry here. The leased devices'
    logs are watched while it runs, and a run a notification banner reached is sent once more, as `devices run`
    does (devices.watched_run): send offers the same commits again then. A rerun that one reaches too, or no
    time left for one, is unmeasured."""
    if lease is None: return send()
    said = lambda text: print(text, file=sys.stderr, flush=True)
    try:
        return devices.watched_run(lease, list(lease.devices), send, {
            "host": host, "work": work, "results": str(Path(root) / ".build/queuekeeper/remote" / name)}, said)
    except devices.Unmeasured:
        print(f"remote: the run is unmeasured; {' and '.join(lease.devices)} "
              f"{'stays' if len(lease.devices) == 1 else 'stay'} reserved", file=sys.stderr)
        return devices.UNMEASURED


class Devices(extensions.Devices):
    def lease(self, keys, owner, words, root=None, *, budget=None, commits=()):
        return lease(keys, owner or devices.default_owner(), words, root=root, budget=budget, commits=commits)

    def carrying(self, held, root, host, work, name, send):
        return carrying(held, root, host, work, name, send)

    def reserved_devices(self, words):
        return named_devices(words)

    def named_devices(self, command, root):
        return devices.named(command, root=root)

    def pool(self, root):
        return simulator_pool.configured(root)
