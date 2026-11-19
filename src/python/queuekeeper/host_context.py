"""Which Mac a command runs on, in which login session, and which Mac another Mac is from there.

Instructions for the owner go wrong on the wrong Mac or in the wrong session: a keychain unlocked
on the workstation does nothing for another Mac, and an SSH alias that the workstation's
~/.ssh/config defines does not resolve in a shell already on another Mac
(vocaro-engineering#65). Before a command asks the owner for anything, it establishes both here,
without connecting anywhere: this Mac's name as receipts record it (offload_host.mac_name), another
Mac's destination from the canonical resolver (remote.configured), what `ssh -G` resolves
that destination to, and whether this Mac's device record marks it as another Mac of another
Mac. The session comes from the process's audit session flags: an SSH login is flagged remote and
has no graphic access, so nothing in it can show a dialog, while a session with graphic access can
(doc/guides/another-mac-setup.md, Keychains on the right Mac).
"""
import ctypes
import os

from . import offload_host, remote

# Audit session flags, as <bsm/audit.h> names them (AU_SESSION_FLAG_*), the same bits AuthSession.h
# gives SessionGetInfo's attributes.
INITIAL, GRAPHIC, TTY, REMOTE, CONSOLE, AUTHENTICATED = 0x0001, 0x0010, 0x0020, 0x1000, 0x2000, 0x4000
FLAG_NAMES = {INITIAL: "initial", GRAPHIC: "graphic access", TTY: "tty", REMOTE: "remote",
              CONSOLE: "console access", AUTHENTICATED: "authenticated"}
# What sshd sets for a login, which a process started from it inherits.
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
# session()'s default: read this process's flags.
READ = object()


class _Mask(ctypes.Structure):
    _fields_ = [("success", ctypes.c_uint), ("failure", ctypes.c_uint)]


class _Terminal(ctypes.Structure):
    _fields_ = [("port", ctypes.c_int32), ("type", ctypes.c_uint32), ("address", ctypes.c_uint32 * 4)]


class _AuditInfo(ctypes.Structure):
    """struct auditinfo_addr, getaudit_addr(2)'s, 48 bytes on macOS 27."""
    _fields_ = [("auid", ctypes.c_uint32), ("mask", _Mask), ("termid", _Terminal), ("asid", ctypes.c_int32),
                ("flags", ctypes.c_uint64)]


def this_mac():
    """This Mac's name, as receipts record it (placement): its LocalHostName."""
    return offload_host.mac_name()


def audit_flags():
    """This process's audit session flags from getaudit_addr(2), or None when they cannot be read."""
    try:
        info = _AuditInfo()
        if ctypes.CDLL(None, use_errno=True).getaudit_addr(ctypes.byref(info), ctypes.sizeof(info)) != 0:
            return None
        return int(info.flags)
    except (OSError, AttributeError):
        return None


def session(flags=READ, environment=None):
    """The login session this process runs in, read and never changed: its kind and the flags it
    was judged by, this process's unless flags gives them, None for flags that could not be read.
    kind is ssh for a session flagged remote, gui for one with graphic access, background for one
    with neither, and unknown when the flags cannot be read. graphicAccess says whether anything in
    the session can show a dialog; None is unknown, which is treated as yes. sshVariables says
    whether the environment holds what sshd sets, which a process started from an SSH shell inherits
    whatever session it then runs in, so it is a note beside the flags."""
    flags = audit_flags() if flags is READ else flags
    environment = os.environ if environment is None else environment
    variables = any(environment.get(name) for name in SSH_VARIABLES)
    if flags is None:
        return {"kind": "unknown", "flags": None, "graphicAccess": None, "remote": None, "consoleAccess": None,
                "sshVariables": variables}
    kind = "ssh" if flags & REMOTE else "gui" if flags & GRAPHIC else "background"
    return {"kind": kind, "flags": [name for bit, name in FLAG_NAMES.items() if flags & bit],
            "graphicAccess": bool(flags & GRAPHIC), "remote": bool(flags & REMOTE),
            "consoleAccess": bool(flags & CONSOLE), "sshVariables": variables}


def farm_mac(destination=None):
    """This Mac's view of one of the farm's Macs, without connecting: its SSH destination, the one given or else
    the first that takes work now (remote.configured), what `ssh -G` resolves it to, and the Mac this
    Mac's device record defers physical devices to, which every qk remote run sets on the second
    Mac's record and so marks this Mac as that Mac's second Mac."""
    destination = destination or remote.configured()
    hostname = (remote.ssh_settings(destination).get("hostname") or destination) if destination else None
    record = offload_host.device_record()
    return {"destination": destination, "hostname": hostname, "deferredTo": record.get("deferredTo")}


def names_mac(hostname, mac):
    """Whether hostname, as `ssh -G` gives it, names the Mac called mac: its LocalHostName, with or
    without .local, case aside, or the loopback."""
    name = (hostname or "").rstrip(".").lower()
    return bool(mac) and name in {mac.lower(), f"{mac.lower()}.local", "localhost", "127.0.0.1", "::1"}


class Location:
    """Where the target Mac is from here: where is "here" or "farm Mac", and mac names it; or
    where is None and refusal says why nothing may be asked of the owner, and what to do instead."""

    def __init__(self, where=None, mac=None, destination=None, refusal=None, note=None):
        self.where, self.mac, self.destination, self.refusal, self.note = where, mac, destination, refusal, note


def locate(here, view, target=None, there=None, unreached=None):
    """Where the target is from this Mac, here, given farm_mac()'s view. target names the Mac
    whose keychain or screen the work needs, or None for another Mac. there is the name another
    Mac gave of itself when it was reached, and unreached why it could not be, if it was
    tried. A destination is used only on the Mac whose SSH configuration defines it."""
    destination, deferred = view.get("destination"), view.get("deferredTo")
    if target is not None and target == here:
        return Location("here", here, note=f"{here} is this Mac, as you named it")
    if destination and names_mac(view.get("hostname"), here):
        return Location("here", here, note=f"{destination} resolves to this Mac")
    if destination:
        if there is None:
            return Location(refusal=f"{destination}, another Mac, could not be reached, so nothing about it can be "
                                    f"checked: {unreached or 'it was not asked'}")
        if there == here:
            return Location("here", here, note=f"{destination} reaches this Mac itself")
        if target is None or target == there:
            return Location("farm Mac", there, destination, note=f"{there} is another Mac, which "
                            f"{destination} reaches from this Mac ({view.get('hostname')})")
        return Location(refusal=(
            f"{target} is neither this Mac, {here}, nor another Mac, {there}, that {destination} reaches: run "
            f"this on {target} itself, in a shell there"))
    if deferred:
        if target is None:
            return Location("here", here, note=f"{here} is another Mac of {deferred}, as its device record says: "
                            f"you are already on the target, where no alias of {deferred}'s names it")
        if target == deferred:
            return Location(refusal=(
                f"{target} is the Mac that sends work to this one, {here}, and this Mac's ~/.ssh/config names no route "
                f"to it: an alias copied from {target} does not resolve here, so run this on {target}, in a shell "
                "there"))
        return Location(refusal=f"{target} is neither this Mac, {here}, nor {deferred}, the Mac that sends work here: "
                                f"run this on {target} itself, in a shell there")
    return Location(refusal=(
        f"this Mac, {here}, sends work to no Mac ({remote.NO_MAC}), and its device record does not mark it as a "
        f"Mac that takes another's. Run this "
        f"on the Mac that sends work to another Mac, or, if this Mac is the one the work needs, with --target "
        f"{here}"))
