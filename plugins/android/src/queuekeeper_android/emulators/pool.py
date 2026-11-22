"""A project's pool of Android emulators, each used only under a reservation in this Mac's device record.

Scripts that start an emulator by a fixed AVD name, or that rely on adb's one-device default, end up sharing one
emulator with another agent, as scripts that name a simulator or use `booted` do on iOS. The pool gives each run an
emulator of its own, as the Apple plugin's simulator pool does.

- A member is an Android Virtual Device named `POOL_LABEL_N`, such as `Pagepour_Pixel_8_API_36_2`, whose kind is its
  system image and hardware profile. Its entry in the device record is keyed by that name and names the device as
  avd:NAME (android_platform.AVD), since the serial adb gives a running emulator lasts only while it runs.
- Each kind has a warmed template, `POOL_LABEL_template`, booted once to the end of its first boot, with the window,
  transition and animator animations off, as instrumented tests want them, and shut down. A first boot of a new
  image took minutes; a member made by cloning the template's folder with APFS's copy-on-write boots in seconds.
- Taking a member reserves a free one of the kind, or the next new one, resets it to the template unless the run
  keeps its state, and starts its emulator headless on a console port of its own, waiting for the boot to finish.
  A boot that fails or passes its limit releases the member and tries another, once.
- Releasing stops the emulator, confirms adb no longer lists it, and releases the reservation; when the stop cannot
  be confirmed, the reservation is kept for recovery, never released over a running emulator.

Each Mac has its own pool and record: an emulator runs on the Mac that started it.
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

from queuekeeper import offload_host
from queuekeeper.resources import process_start
from queuekeeper_mobile import devices as device_record
from .. import adb, android_platform, sdk  # noqa: F401 -- android_platform switches the Android platform on
from . import avd

# How long a take reserves a member for unless the caller says otherwise, and the boot's limit.
HOLD = timedelta(hours=2)
BOOT_TIMEOUT = 600
# A template's first boot, which installs and optimizes the image's packages.
TEMPLATE_BOOT_TIMEOUT = 1200
# The settings a template is given before it is shut down: animations off, as Espresso's guide asks for.
QUIET = (("global", "window_animation_scale", "0"), ("global", "transition_animation_scale", "0"),
         ("global", "animator_duration_scale", "0"))
# The file in a template's folder that says its first boot finished and its settings were made.
WARMED = "queuekeeper-template.json"
# The image and profile a take uses unless told otherwise.
IMAGE = "system-images;android-36;google_apis;arm64-v8a"
DEVICE = "pixel_8"
# A member's own number at the end of its name.
NUMBER = re.compile(r"_([1-9][0-9]*)")
ATTEMPTS = 2
# Keep a running member for at least as long as the simulator reaper keeps its counterpart.
REAP_AFTER = timedelta(hours=2).total_seconds()
REAP_QUERY_SECONDS = 5
REAP_STOP_SECONDS = 10


def configured(root):
    """The pool a project's queuekeeper.json names, or None."""
    try:
        return json.loads((Path(root) / "queuekeeper.json").read_text()).get("emulatorPool")
    except (OSError, ValueError, AttributeError):
        return None


def word(text):
    """text as a word an AVD's name may hold: letters, digits, dots, underscores and dashes."""
    return re.sub(r"[^A-Za-z0-9.-]+", "_", text).strip("_")


def label(image, device):
    """The words a kind's members carry after the pool's name, such as Pixel_8_API_36 for a google_apis image, with
    the image's tag after them when it is another, such as Pixel_8_API_36_playstore."""
    parts = image.split(";")
    api = parts[1].removeprefix("android-") if len(parts) > 1 else "unknown"
    tag = parts[2] if len(parts) > 2 else ""
    profile = "_".join(piece.capitalize() if piece.isalpha() else piece for piece in device.split("_"))
    tail = "" if tag in ("google_apis", "") else "_" + tag.removeprefix("google_apis_")
    return word(f"{profile}_API_{api}{tail}")


@dataclass
class Lease:
    """A member this owner holds, and its emulator once started."""
    key: str
    revision: int
    serial: str = None
    port: int = None
    process: object = field(default=None, repr=False)
    created: bool = False


class Pool:
    """The pool name, whose reservations owner holds in record."""

    def __init__(self, name, owner, *, record=None, folder=None, logs=None, said=None, hold=HOLD):
        if not name: raise ValueError("name the pool: --pool, or emulatorPool in queuekeeper.json")
        self.name, self.owner, self.hold = word(name), owner, hold
        self.record = record or device_record.Record()
        self.folder = Path(folder) if folder else avd.home()
        self.logs = Path(logs) if logs else self.folder.parent / "queuekeeper-emulator-logs"
        self.said = said or (lambda text: print(f"emulator-pool: {text}", file=sys.stderr, flush=True))

    # Names.
    def template_name(self, image, device):
        return f"{self.name}_{label(image, device)}_template"

    def members(self, image, device, listed=None):
        """{number: AVD name} of the kind's members."""
        prefix = f"{self.name}_{label(image, device)}"
        listed = avd.listed(self.folder) if listed is None else listed
        return {int(found.group(1)): name for name in listed
                if name.startswith(prefix) and (found := NUMBER.fullmatch(name[len(prefix):]))}

    # Reservations.
    def _release_by(self, hold=None):
        return (datetime.now(timezone.utc) + (self.hold if hold is None else hold)).isoformat()

    def _reserve(self, key, purpose, **details):
        if not self.owner: raise ValueError(f"name the owner whose reservation takes {key}")
        current = self.record.entry(key)
        try:
            start = process_start(os.getpid())
        except OSError:
            start = None
        if start is not None: details[device_record.HOLDER] = {"pid": os.getpid(), "start": start}
        entry = self.record.reserve(key, owner=self.owner, revision=(current or {}).get("revision", 0),
                                    device=f"avd:{key}", purpose=purpose, release_by=self._release_by(),
                                    details=details)
        return entry["revision"]

    def _free(self, key, running):
        entry = self.record.entry(key)
        return key not in running and (entry is None or device_record.condition(entry) == "released")

    # The template.
    def template(self, image, device, *, purpose):
        """The kind's warmed template's name, made and warmed first when it has none: its first boot runs once, under
        a reservation of its own, so that two sessions never warm one together."""
        name = self.template_name(image, device)
        deadline = time.monotonic() + TEMPLATE_BOOT_TIMEOUT + BOOT_TIMEOUT
        while True:
            listed = avd.listed(self.folder)
            if name in listed and (Path(listed[name]["path"]) / WARMED).exists(): return name
            try:
                revision = self._reserve(name, f"warm the template for {purpose}")
                break
            except device_record.Refused as error:
                # Another run warms it now, or copies it: wait for that to end, as a member's take waits for nothing.
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"the template {name} stayed held: {error}") from None
                time.sleep(5)
        lease = Lease(name, revision)
        try:
            if name not in avd.listed(self.folder):
                self.said(f"CREATE template {name} from {image} as {device}")
                avd.create(name, image, device, folder=self.folder)
            self.said(f"WARM {name}: its first boot, limit {TEMPLATE_BOOT_TIMEOUT}s")
            started = self.start(lease, timeout=TEMPLATE_BOOT_TIMEOUT)
            if not started:
                raise RuntimeError(f"the template {name}'s first boot did not finish within {TEMPLATE_BOOT_TIMEOUT}s")
            # The settings service writes them to the disk a moment later; the sync and wait keep the stop from
            # losing them, as an immediate stop did (2026-11-18).
            time.sleep(3)
            if not self.stop(lease):
                raise RuntimeError(f"the template {name}'s emulator did not stop after its first boot")
            (Path(avd.listed(self.folder)[name]["path"]) / WARMED).write_text(json.dumps(
                {"image": image, "device": device, "warmedAt": device_record.now(), "by": self.owner}) + "\n")
        except BaseException:
            self.release(lease, outcome=f"Warming failed for {purpose}")
            raise
        self.release(lease, outcome=f"Warmed for {purpose}")
        return name

    # Starting and stopping.
    def start(self, lease, *, timeout=BOOT_TIMEOUT, window=False):
        """Start lease's emulator on a free port and wait for its boot; whether it booted. Its serial, port and
        process go in the lease and its entry."""
        self.logs.mkdir(parents=True, exist_ok=True)
        log = self.logs / f"{lease.key}.log"
        with avd.port_lock(self.folder):
            held = {entry.get("port") for entry in self.record.load()["devices"].values()
                    if isinstance(entry, dict) and entry.get("port") and device_record.condition(entry) != "released"}
            port = avd.free_port(held)
            lease.process = avd.start(lease.key, port, log=log, window=window, folder=self.folder)
            lease.port, lease.serial = port, f"emulator-{port}"
            avd.listening(port, lease.process)
        try: start = process_start(lease.process.pid)
        except OSError: start = None
        lease.revision = self.record.update(lease.key, owner=self.owner, revision=lease.revision, serial=lease.serial,
                                            port=port, emulatorProcess={"pid": lease.process.pid, "start": start},
                                            emulatorLog=str(log))["revision"]
        deadline = time.monotonic() + timeout
        # adb does not list the emulator until its console is up; the boot is waited for once it does.
        while time.monotonic() < deadline and lease.process.poll() is None:
            if adb.wait_booted(lease.serial, min(30, max(1, deadline - time.monotonic()))): break
        else:
            return False
        # The serial is this member's only when its own emulator answers there.
        answering = avd.name_of(lease.serial)
        if answering != lease.key:
            self.said(f"{lease.serial} answers as {answering or 'nothing'}, not {lease.key}: another emulator took "
                      "its port")
            return False
        self.quiet(lease.serial)
        return True

    def quiet(self, serial):
        """Animations off, as instrumented tests want them, written through to the disk."""
        for namespace, key, value in QUIET:
            adb.shell(serial, "settings", "put", namespace, key, value)
        adb.shell(serial, "sync", check=False)

    def stop(self, lease):
        """Stop lease's emulator; whether adb no longer lists it and no emulator of its AVD runs."""
        serial = lease.serial or next((serial for serial, name in avd.running().items() if name == lease.key), None)
        if serial is None: return lease.key not in avd.running().values()
        stopped = avd.stop(serial, lease.process)
        return stopped and lease.key not in avd.running().values()

    def release(self, lease, *, outcome):
        """Stop the emulator and release the reservation once the stop is confirmed; otherwise keep it for recovery
        and return False."""
        try:
            current = self.record.entry(lease.key)
            if current is None or current.get("ownerChatID") != self.owner or \
                    device_record.condition(current) == "released":
                raise device_record.Refused("the reservation changed")
            if lease.serial is None and current.get("serial"): lease.serial = current["serial"]
            if not self.stop(lease):
                raise device_record.Refused("its emulator still runs")
            self.record.release(lease.key, owner=self.owner, revision=current["revision"], outcome=outcome,
                                restoration="Confirmed the emulator stopped")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.said(f"{lease.key}: reservation kept for recovery: {error}")
            return False
        return True

    # Taking.
    def take(self, image=IMAGE, device=DEVICE, *, purpose, keep_state=False, attempts=ATTEMPTS,
             boot_timeout=BOOT_TIMEOUT, window=False, exclude=()):
        """A member of the kind, reserved, reset from the template unless keep_state, started and booted; its lease.
        A member whose boot fails is released and another tried, attempts in all."""
        template = self.template(image, device, purpose=purpose)
        tried = set(exclude)
        for attempt in range(attempts):
            lease = self._claim(image, device, template, purpose=purpose, keep_state=keep_state, exclude=tried)
            self.said(f"BOOT {lease.key}, limit {boot_timeout}s")
            started = time.monotonic()
            try:
                booted = self.start(lease, timeout=boot_timeout, window=window)
            except BaseException:
                self.release(lease, outcome=f"Stopped before {purpose}")
                raise
            if booted:
                self.said(f"TOOK {lease.key} as {lease.serial}, booted in {round(time.monotonic() - started)}s")
                return lease
            tried.add(lease.key)
            self.release(lease, outcome=f"Its boot failed or passed {boot_timeout}s before {purpose}")
            self.said(f"{lease.key} did not boot within {boot_timeout}s; "
                      + ("trying another member" if attempt + 1 < attempts else "no member left to try"))
        raise TimeoutError(f"no member of {label(image, device)} booted in {attempts} tries")

    def _claim(self, image, device, template, *, purpose, keep_state, exclude):
        listed = avd.listed(self.folder)
        running = set(avd.running().values())
        members = self.members(image, device, listed)
        for number, name in sorted(members.items()):
            if name in exclude or not self._free(name, running): continue
            try:
                revision = self._reserve(name, purpose, keptState=bool(keep_state))
            except device_record.Refused:
                continue
            lease = Lease(name, revision)
            if not keep_state:
                try:
                    avd.delete(name, folder=self.folder)
                    avd.clone(template, name, folder=self.folder)
                except BaseException:
                    self.release(lease, outcome=f"Its reset failed before {purpose}")
                    raise
            return lease
        # A new member takes the lowest number no AVD or entry has; two sessions creating at once each reserve the
        # name first, and the one refused tries the next.
        number = 1
        while True:
            name = f"{self.name}_{label(image, device)}_{number}"
            if number in members or name in exclude or not self._free(name, running):
                number += 1
                continue
            try:
                revision = self._reserve(name, purpose, created=True)
            except device_record.Refused:
                number += 1
                continue
            break
        lease = Lease(name, revision, created=True)
        try:
            avd.clone(template, name, folder=self.folder)
        except BaseException:
            self.record.release(name, owner=self.owner, revision=revision, outcome=f"Not created for {purpose}",
                                restoration="Nothing was started")
            raise
        self.said(f"CREATED {name} from {template}")
        return lease

    def held(self, key=None, serial=None):
        """This owner's lease on a member named by its AVD or its serial."""
        for name, entry in self.record.load()["devices"].items():
            if entry.get("ownerChatID") != self.owner or device_record.condition(entry) == "released": continue
            if name == key or (serial and entry.get("serial") == serial):
                return Lease(name, entry["revision"], entry.get("serial"), entry.get("port"))
        raise device_record.Refused(f"{self.owner} holds no member {key or serial}")

    def release_mine(self, outcome):
        """Release every member of the pool this owner holds: [(key, whether released)]."""
        found = []
        for name, entry in sorted(self.record.load()["devices"].items()):
            if not name.startswith(self.name + "_") or entry.get("ownerChatID") != self.owner: continue
            if device_record.condition(entry) != "held": continue
            found.append((name, self.release(Lease(name, entry["revision"], entry.get("serial")), outcome=outcome)))
        return found

    def reap(self, *, dry_run=False):
        """Report stale reservations of this pool. Stop only an emulator proved to be the recorded run."""
        if any(os.environ.get(key) for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS",
                                              "ANDROID_ADB_SERVER_PORT")):
            raise ValueError("reap needs the default local adb server to identify this Mac's emulators")
        entries = self.record.load()["devices"]
        results = []
        for name, avd_entry in avd.listed(self.folder).items():
            image = avd.image_package(avd_entry["image"])
            profile = avd_entry["device"]
            entry = entries.get(name)
            if not image or not profile or name not in self.members(image, profile, [name]).values():
                continue
            if not entry or entry.get("device") != f"avd:{name}" or entry.get("state") == "released":
                continue
            try:
                if dry_run:
                    booted, serial, emulator = self._reap_check(name, entries)
                    if booted: self._reap_console(name, serial, emulator)
                    elif self._reap_listed(serial):
                        raise device_record.Refused(f"adb still lists {serial}")
                    said = "would stop and release" if booted else "would release its stopped emulator"
                else:
                    self._reap_stop(name, entry)
                    self.record.recover(name, by="emulator-pool reap", revision=entry["revision"],
                                        verified="Expired reservation, recorded holder processes ended and emulator stopped")
                    said = "released after confirming its emulator stopped"
            except device_record.Refused as error:
                results.append((f"{name}: kept: {error}", False))
            except (OSError, adb.Failed, sdk.Missing, TimeoutError) as error:
                results.append((f"{name}: could not reap: {error}", True))
            else:
                results.append((f"{name}: {said}", False))
        return results

    def _reap_check(self, name, entries):
        """Whether the recorded emulator runs, with its observed serial and identity, or why it is kept."""
        entry = entries[name]
        if device_record.condition(entry) != "held" or entry.get("activeRun"):
            raise device_record.Refused("its reservation is active or needs attended recovery")
        due = device_record.moment(entry.get("expectedReleaseBy"))
        if due is None or due > datetime.now(timezone.utc):
            raise device_record.Refused("its expected release has not passed or cannot be read")
        serial, port = entry.get("serial"), entry.get("port")
        if type(port) is not int or port not in avd.PORTS or serial != f"emulator-{port}":
            raise device_record.Refused("its emulator serial and port cannot be verified")
        for key, other in entries.items():
            if key != name and (other.get("port") == port or other.get("device") == entry["device"]) and \
                    device_record.condition(other) != "released":
                raise device_record.Refused(f"reservation {key} also holds its AVD or port")
        emulator = entry.get("emulatorProcess")
        owner = entry.get("ownerChatID")
        owned = entry.get("ownedProcesses", [])
        if not isinstance(owned, list) or not isinstance(owner, str) or len(owner.strip()) < 8:
            raise device_record.Refused("its owner or recorded owned processes cannot be checked")
        holders = [entry.get(device_record.HOLDER), *owned]
        if any(not isinstance(item, dict) or type(item.get("pid")) is not int or item["pid"] <= 0
               or offload_host.started(item.get("start")) is None for item in [emulator, *holders]):
            raise device_record.Refused("its emulator or holder has no verifiable process identity")
        if time.time() - offload_host.started(emulator["start"]) < REAP_AFTER:
            raise device_record.Refused("its recorded emulator is less than two hours old")
        table = offload_host.process_table(offload_host.listing(list(offload_host.ENVIRONMENTS), REAP_QUERY_SECONDS))
        own = table.get(os.getpid(), {})
        if "PATH=" not in own.get("args", ""):
            raise device_record.Refused("the process census could not verify which processes still use it")
        identities = {pid: row["start"] for pid, row in table.items()}
        if any(offload_host.process_state(item, identities) != "ended" for item in holders):
            raise device_record.Refused("one of its recorded holder processes is still running")
        booted = offload_host.process_state(emulator, identities) == "running"
        for pid, process in table.items():
            if process["uid"] != os.getuid() or pid == os.getpid() or \
                    (booted and emulator["pid"] in offload_host.lineage(table, pid)):
                continue
            if any(word in process["args"] for word in (name, serial, owner)):
                raise device_record.Refused(f"PID {pid} still names its AVD, serial or owner")
        if not booted and not self._reap_stopped(serial, emulator):
            raise device_record.Refused("its recorded process ended but its serial or ports cannot be shown stopped")
        return booted, serial, emulator

    def _reap_stopped(self, serial, emulator):
        return (avd.ports_stopped(adb.emulator_port(serial))
                and offload_host.process_state(emulator, offload_host.census([emulator["pid"]])) == "ended")

    def _reap_listed(self, serial):
        listing = adb.run(["devices", "-l"], timeout=REAP_QUERY_SECONDS).stdout
        if "List of devices attached" not in listing.splitlines():
            raise adb.Failed("adb did not list its devices")
        return serial in adb.listed(listing)

    def _reap_console(self, name, serial, emulator, *, stop=False):
        """Name and stop on the same connection to the recorded emulator process."""
        with avd.owned_console(adb.emulator_port(serial), emulator, REAP_QUERY_SECONDS) as (connection, stream):
            connection.sendall(b"avd name\n")
            if avd.console_reply(stream) != [name.encode()]:
                raise device_record.Refused(f"{serial} does not answer for {name}")
            if stop: connection.sendall(b"kill\n")

    def _reap_stop(self, name, entry):
        """Request shutdown under the record's lock and wait outside it. The reservation remains held."""
        with self.record._exclusive():
            entries = self.record.load()["devices"]
            if entries.get(name) != entry:
                raise device_record.Stale("its reservation changed before the stop")
            booted, serial, emulator = self._reap_check(name, entries)
            if booted: self._reap_console(name, serial, emulator, stop=True)
        deadline = time.monotonic() + REAP_STOP_SECONDS
        while not self._reap_stopped(serial, emulator) or self._reap_listed(serial):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{serial} did not confirm its stop")
            time.sleep(0.2)

    def status(self):
        """One line per member and template of the pool: its kind, whether its emulator runs, and who holds it."""
        listed, running = avd.listed(self.folder), avd.running()
        names = {name: serial for serial, name in running.items()}
        value = self.record.load()["devices"]
        lines = []
        for name, item in sorted(listed.items()):
            if not name.startswith(self.name + "_"): continue
            entry = value.get(name) or {}
            state = device_record.condition(entry) if entry else "never reserved"
            holder = f" by {entry.get('ownerChatID')}" if state != "released" and entry else ""
            image = avd.image_package(item["image"])
            lines.append(f"{name}: {image or 'unknown image'} as {item['device'] or 'unknown profile'}; "
                         f"{'running as ' + names[name] if name in names else 'stopped'}; {state}{holder}")
        return lines


def main(argv, project=None):
    parser = argparse.ArgumentParser(prog="qk emulator-pool", description="Take, release, reap or list members of the "
                                     "project's emulator pool under device-record reservations.")
    parser.add_argument("--pool", default=configured(Path.cwd()) or (configured(project) if project else None),
                        help="the pool's name (default: the project's emulatorPool in queuekeeper.json)")
    parser.add_argument("--owner", default=device_record.default_owner(),
                        help=f"the session (default: {device_record.OWNER}, else the agent session)")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="every member and template, its kind, its emulator and its holder")
    take = actions.add_parser("take", help="reserve, reset and boot a member; prints its AVD and serial as JSON")
    take.add_argument("--image", default=IMAGE, help=f"the system image package (default {IMAGE})")
    take.add_argument("--device", default=DEVICE, help=f"the hardware profile, as avdmanager names it (default {DEVICE})")
    take.add_argument("--purpose", required=True)
    take.add_argument("--keep-state", action="store_true", help="boot the member as the last holder left it")
    take.add_argument("--boot-timeout", type=float, default=BOOT_TIMEOUT)
    take.add_argument("--window", action="store_true", help="show the emulator's window")
    template = actions.add_parser("template", help="make and warm the kind's template, if it has none")
    template.add_argument("--image", default=IMAGE)
    template.add_argument("--device", default=DEVICE)
    release = actions.add_parser("release", help="stop a member's emulator and release it")
    target = release.add_mutually_exclusive_group(required=True)
    target.add_argument("--avd", help="the member's AVD name")
    target.add_argument("--serial", help="its emulator's adb serial")
    release.add_argument("--outcome", required=True)
    mine = actions.add_parser("release-mine", help="stop and release every member the owner holds")
    mine.add_argument("--outcome", default="Released at the session's end")
    reap = actions.add_parser("reap", help="stop and release abandoned pool members on this Mac")
    reap.add_argument("--dry-run", action="store_true", help="report eligible members without stopping or releasing them")
    args = parser.parse_args(argv)
    try:
        pool = Pool(args.pool, args.owner)
        if args.action == "list":
            for line in pool.status(): print(line)
        elif args.action == "template":
            print(pool.template(args.image, args.device, purpose="qk emulator-pool template"))
        elif args.action == "take":
            lease = pool.take(args.image, args.device, purpose=args.purpose, keep_state=args.keep_state,
                              boot_timeout=args.boot_timeout, window=args.window)
            print(json.dumps({"avd": lease.key, "serial": lease.serial, "port": lease.port}))
        elif args.action == "release":
            lease = pool.held(key=args.avd, serial=args.serial)
            return 0 if pool.release(lease, outcome=args.outcome) else 1
        elif args.action == "release-mine":
            results = pool.release_mine(args.outcome)
            for key, released in results: print(f"{key}: {'released' if released else 'kept for recovery'}")
            return 0 if all(released for _key, released in results) else 1
        elif args.action == "reap":
            results = pool.reap(dry_run=args.dry_run)
            for line, _failed in results: print(line)
            if not results: print(f"{pool.name}: no recorded pool members to reap")
            return 1 if any(failed for _line, failed in results) else 0
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, sdk.Missing) as error:
        print(f"emulator-pool: {error}", file=sys.stderr)
        return 2
