"""Android Virtual Devices: the emulators this Mac's account has, made, copied, started and stopped by name.

An AVD is NAME.ini in the AVD folder, which names NAME.avd beside it, holding config.ini and the emulator's disks.
A copy is the folder cloned with APFS's copy-on-write (`cp -c`), as the Apple plugin copies a warmed simulator's
data, with the paths inside its two .ini files rewritten to the new name; the copy keeps the original's first boot,
so it boots in seconds rather than minutes.

An emulator is started on a console port of its own, even from 5554 to 5682, and adb names it `emulator-PORT`. A
port is chosen by asking adb which are in use and by the record's reservation of the AVD, never by the emulator's own
choice, which two emulators starting at once can make alike.
"""
import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time

from .. import adb, sdk

PORTS = range(5554, 5684, 2)
# What a started emulator is told: no window, unless a run asks to watch it, no audio, no boot animation, and no
# snapshot saved on exit, so that a member's next boot starts from what its reset left.
HEADLESS = ("-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot-save")
CONFIG_KEYS = ("avd.ini.displayname", "AvdId")


def home(environ=None, *, account_home=None):
    """The folder AVDs are kept in: ANDROID_AVD_HOME, else ANDROID_USER_HOME's avd, else ~/.android/avd."""
    environ = os.environ if environ is None else environ
    if environ.get("ANDROID_AVD_HOME"): return Path(environ["ANDROID_AVD_HOME"]).expanduser()
    if environ.get("ANDROID_USER_HOME"): return Path(environ["ANDROID_USER_HOME"]).expanduser() / "avd"
    return (Path.home() if account_home is None else Path(account_home)) / ".android/avd"


def ini(path):
    """key=value lines as a dict."""
    values = {}
    try: text = Path(path).read_text(errors="replace")
    except OSError: return values
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def write_ini(path, values):
    Path(path).write_text("".join(f"{key}={value}\n" for key, value in values.items()))


def listed(folder=None):
    """{name: {"path", "image", "device", "config"}} of each AVD in folder, by its NAME.ini."""
    folder = folder or home()
    found = {}
    for entry in sorted(Path(folder).glob("*.ini")):
        pointer = ini(entry)
        path = Path(pointer.get("path") or folder / f"{entry.stem}.avd")
        config = ini(path / "config.ini")
        found[entry.stem] = {"path": str(path), "image": config.get("image.sysdir.1", ""),
                             "device": config.get("hw.device.name", ""), "config": config}
    return found


def create(name, image, device, *, folder=None, timeout=300):
    """Make the AVD name from the system image package image, such as system-images;android-36;google_apis;
    arm64-v8a, as the hardware profile device, such as pixel_8."""
    command = [sdk.tool("avdmanager"), "create", "avd", "--name", name, "--package", image, "--device", device]
    if folder: command += ["--path", str(Path(folder) / f"{name}.avd")]
    done = subprocess.run(command, input="no\n", capture_output=True, text=True, timeout=timeout,
                          env=sdk.environment())
    if done.returncode != 0:
        raise adb.Failed(f"avdmanager create avd {name} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-800:]}")
    return listed(folder)[name]


def clone(source, name, *, folder=None):
    """Copy the AVD source as name, cloning its folder with copy-on-write, and rewrite its .ini files to name it."""
    folder = Path(folder or home())
    original = listed(folder)[source]
    target = folder / f"{name}.avd"
    if target.exists() or (folder / f"{name}.ini").exists():
        raise FileExistsError(f"an AVD named {name} already exists in {folder}")
    subprocess.run(["cp", "-c", "-R", original["path"], str(target)], check=True, capture_output=True, timeout=600)
    for lock in target.glob("*.lock"):
        shutil.rmtree(lock, True) if lock.is_dir() else lock.unlink(missing_ok=True)
    config = ini(target / "config.ini")
    for key in CONFIG_KEYS:
        if key in config: config[key] = name
    write_ini(target / "config.ini", config)
    pointer = ini(folder / f"{source}.ini")
    pointer.update({"path": str(target), "path.rel": f"avd/{name}.avd"})
    write_ini(folder / f"{name}.ini", pointer)
    return listed(folder)[name]


def delete(name, *, folder=None):
    folder = Path(folder or home())
    entry = listed(folder).get(name)
    if entry: shutil.rmtree(entry["path"], True)
    (folder / f"{name}.ini").unlink(missing_ok=True)


def wipe(name, *, folder=None):
    """Put the AVD name back to its first boot's state: the user data and caches its runs wrote are deleted, and the
    emulator makes them again from the image on its next boot (as -wipe-data does, without booting)."""
    path = Path(listed(folder)[name]["path"])
    for leaf in ("userdata-qemu.img", "userdata-qemu.img.qcow2", "cache.img", "cache.img.qcow2",
                 "sdcard.img.qcow2", "encryptionkey.img.qcow2"):
        (path / leaf).unlink(missing_ok=True)
    shutil.rmtree(path / "snapshots", True)


@contextlib.contextmanager
def port_lock(folder=None, timeout=120):
    """This account's lock on choosing an emulator's port, held from the choice until its emulator listens there: two
    sessions that start emulators at once otherwise choose the same free port, and the second emulator, which cannot
    take it, lets the first answer for both (2026-11-18)."""
    path = Path(folder or home()).parent / "queuekeeper-emulator-ports.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise TimeoutError(f"{path} stayed locked for {timeout}s")
                time.sleep(0.2)
        yield
    finally:
        os.close(descriptor)


def listening(port, process=None, timeout=60):
    """Wait until something listens on port, the emulator's console; False when timeout passed first or process
    ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _closed(port): return True
        if process is not None and process.poll() is not None: return False
        time.sleep(0.25)
    return False


def name_of(serial):
    """The AVD whose emulator answers at serial, by asking its console, or None."""
    try:
        text = adb.run(["emu", "avd", "name"], serial=serial, timeout=10).stdout
    except adb.Failed:
        return None
    return text.splitlines()[0].strip() if text.strip() else None


def free_port(used=()):
    """An even console port nothing listens on and adb lists no emulator at, and none of used."""
    try: listed_serials = adb.devices()
    except (adb.Failed, sdk.Missing): listed_serials = {}
    taken = {adb.emulator_port(serial) for serial in listed_serials} | set(used)
    for port in PORTS:
        if port in taken or port + 1 in taken: continue
        if all(_closed(candidate) for candidate in (port, port + 1)): return port
    raise RuntimeError("every emulator console port from 5554 to 5682 is in use")


def _closed(port):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def start(name, port, *, log, window=False, wipe_data=False, extra=(), folder=None):
    """Start the AVD name's emulator on port, writing its output to log; its process. It is in a process group of
    its own, so that stop ends its qemu children with it, and outlives the command that started it, as a pool member
    taken for a session does."""
    command = [sdk.tool("emulator"), "-avd", name, "-port", str(port), *(() if window else HEADLESS),
               *(["-wipe-data"] if wipe_data else []), *extra]
    environment = sdk.environment()
    if folder: environment["ANDROID_AVD_HOME"] = str(folder)
    with open(log, "ab") as output:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                env=environment, start_new_session=True)


def running():
    """{serial: AVD name} of each emulator adb lists, by asking each its name."""
    found = {}
    try: listed_serials = adb.devices()
    except (adb.Failed, sdk.Missing): return found
    for serial in listed_serials:
        if adb.emulator_port(serial) is None: continue
        name = name_of(serial)
        if name: found[serial] = name
    return found


def listed_serial(serial):
    """Whether adb lists serial now, in any state; None when adb could not be asked."""
    try: return serial in adb.devices()
    except (adb.Failed, sdk.Missing): return None


def stop(serial, process=None, timeout=60, settle=15):
    """Stop serial's emulator, by its console's kill and then by signal; whether it has ended and adb no longer
    lists it. The launcher can end before its qemu child, and adb lists an emulator that is shutting down as offline
    for some seconds, so both are waited for."""
    ended = lambda: process is None or process.poll() is not None
    try: adb.run(["emu", "kill"], serial=serial, timeout=15, check=False)
    except adb.Failed: pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not (ended() and listed_serial(serial) is False):
        time.sleep(1)
    if process is not None and process.poll() is None:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(10)
    deadline = time.monotonic() + settle
    while listed_serial(serial) and time.monotonic() < deadline: time.sleep(1)
    return ended() and listed_serial(serial) is False


def image_package(sysdir):
    """The sdkmanager package a config's image.sysdir.1 names: system-images/android-36/google_apis/arm64-v8a/ is
    system-images;android-36;google_apis;arm64-v8a."""
    return ";".join(part for part in re.split(r"[\\/]", sysdir or "") if part)
