"""adb, by serial: the devices this Mac's adb server lists, and commands on one of them.

Every command names its device with -s SERIAL. A script that relies on adb's one-device default, or on
ANDROID_SERIAL, ends up on whichever device or emulator is attached, another agent's included, which is the Android
form of the Apple plugin's `booted` problem.

A serial says what a device is: `emulator-PORT` is an emulator on this Mac, HOST:PORT and an
`adb-SERIAL-RANDOM._adb-tls-connect._tcp` service name are a physical device reached over the network, and anything
else is a physical device on a cable, by the serial number its USB descriptor gives.
"""
import re
import subprocess
import time

from . import sdk

EMULATOR = re.compile(r"emulator-(\d+)")
# A device reached over Wi-Fi: by address and port, or by the mDNS service name of wireless debugging, whose first
# part is the device's own serial number.
NETWORK = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|[\w.-]+):\d+")
WIRELESS = re.compile(r"adb-(?P<serial>[\w]+)-\w+\._adb-tls-connect\._tcp\.?")
# A physical device's serial on a cable, as adb prints it: letters and digits, at least six.
HARDWARE = re.compile(r"[A-Za-z0-9]{6,}")
# Seconds a query of one device has, unless the caller names its own.
TIMEOUT = 30


class Failed(RuntimeError):
    """An adb command that did not finish or exited nonzero."""


def emulator_port(serial):
    """The console port of the emulator serial names, or None for a physical device."""
    found = EMULATOR.fullmatch(serial or "")
    return int(found.group(1)) if found else None


def physical(serial):
    """Whether serial names a physical device rather than an emulator."""
    if not isinstance(serial, str) or not serial or emulator_port(serial) is not None: return False
    return bool(NETWORK.fullmatch(serial) or WIRELESS.fullmatch(serial) or HARDWARE.fullmatch(serial))


def hardware_serial(serial):
    """The device's own serial number when serial names it, by cable or by wireless debugging; None for an address,
    which names no device, and for an emulator."""
    found = WIRELESS.fullmatch(serial or "")
    if found: return found.group("serial")
    if emulator_port(serial) is None and not NETWORK.fullmatch(serial or "") and HARDWARE.fullmatch(serial or ""):
        return serial
    return None


def run(arguments, *, serial=None, timeout=TIMEOUT, check=True, text=True, environ=None):
    """adb with arguments, on serial's device when given; its CompletedProcess."""
    command = [sdk.tool("adb", environ), *(["-s", serial] if serial else []), *arguments]
    try:
        done = subprocess.run(command, capture_output=True, text=text, timeout=timeout, env=sdk.environment(environ),
                              **({"errors": "replace"} if text else {}))
    except subprocess.TimeoutExpired:
        raise Failed(f"{' '.join(command[1:])} did not finish within {timeout:g}s") from None
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[-500:] if text else ""
        raise Failed(f"{' '.join(command[1:])} exited {done.returncode}: {detail}")
    return done


def start_server(environ=None):
    """Start this Mac's adb server, if none runs, in a session of its own.

    adb's first client starts the server as its child, and a lane's supervision stops whatever its command left
    running when the command ends: a server one lane started was stopped under every other session's adb, and an
    instrumentation another lane was streaming through it ended mid-test. So queuekeeper starts the server before a
    lane's command starts, from its own process (lane.prepare), where no lane's supervision counts it as the lane's.
    Returns whether a server answers."""
    try:
        command = [sdk.tool("adb", environ), "start-server"]
    except sdk.Missing:
        return False
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=sdk.environment(environ), start_new_session=True)
        return process.wait(30) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def shell(serial, *words, timeout=TIMEOUT, check=True):
    """The standard output of a shell command on serial's device, stripped."""
    return run(["shell", *words], serial=serial, timeout=timeout, check=check).stdout.strip()


def listed(text):
    """{serial: {"state", "physical", "way", fields...}} from `adb devices -l` output. way is "emulator", "cable"
    or "network"; fields are adb's own, such as model, product, device, usb and transport_id."""
    found = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith(("List of devices", "*")): continue
        words = line.split()
        if len(words) < 2: continue
        serial, state = words[0], words[1]
        fields = dict(word.split(":", 1) for word in words[2:] if ":" in word)
        way = "emulator" if emulator_port(serial) is not None else \
            "network" if NETWORK.fullmatch(serial) or WIRELESS.fullmatch(serial) else "cable"
        found[serial] = {"state": state, "physical": physical(serial), "way": way, **fields}
    return found


def devices(environ=None):
    """What `adb devices -l` lists now (listed)."""
    return listed(run(["devices", "-l"], environ=environ).stdout)


def prop(serial, name, timeout=TIMEOUT):
    """A system property of serial's device, or "" when unset."""
    return shell(serial, "getprop", name, timeout=timeout)


def booted(serial):
    """Whether serial's device has finished booting: adb reaches it and sys.boot_completed is 1."""
    try: return prop(serial, "sys.boot_completed", timeout=10) == "1"
    except Failed: return False


def wait_booted(serial, timeout, poll=2, clock=time.monotonic, sleep=time.sleep):
    """Wait until serial's device has booted and its package manager answers; False when timeout passed first."""
    deadline = clock() + timeout
    while clock() < deadline:
        if booted(serial):
            try:
                if shell(serial, "pm", "path", "android", timeout=10).startswith("package:"): return True
            except Failed:
                pass
        sleep(poll)
    return False


def install(serial, apk, *, timeout=300, test=False):
    """Install apk on serial's device, replacing an earlier install and granting its runtime permissions."""
    run(["install", "-r", "-g", *(["-t"] if test else []), str(apk)], serial=serial, timeout=timeout)


def uninstall(serial, package, timeout=60):
    """Uninstall package from serial's device; whether it was there."""
    return run(["uninstall", package], serial=serial, timeout=timeout, check=False).returncode == 0
