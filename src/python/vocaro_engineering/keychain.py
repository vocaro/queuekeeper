"""Keychain recovery on the right Mac, and a check of key access where the automation runs.

`eng keychain recover` is for the owner, and unlocks nothing. It says which Mac it runs on and
which Mac's keychain the work needs (host_context.locate), checks that the exact keychain file is
there, and prints commands to copy: each runs only on the Mac it names, as `scutil --get
LocalHostName` reads it, and otherwise says where it is and stops. The command that goes through
an SSH alias is printed only on the Mac whose ~/.ssh/config defines the alias, and on the target
itself only the local command is. `security unlock-keychain` then asks for the password in the
owner's own Terminal, and nothing of the toolkit reads it, stores it or passes it on.

`eng keychain check` runs where the automation that uses the key runs, such as on the second Mac
through `eng remote admit`. It records the session it ran in, the keychain's status, whether the
signing identity is there and whether signing with it succeeded, each separately, since an
unlocked keychain and a listed identity do not show that the key can be used. It runs nothing
that could show a dialog: only in a session without graphic access, as an SSH login is, does it
run `security` or `codesign` at all, with no input, each within its time limit. A check that could
not be made is unmeasured, never passed (guides/shared-host.md, Keychains on the right Mac).
"""
import difflib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from . import evidence, host_context, offload_host, remote

DEFAULT = "~/Library/Keychains/login.keychain-db"
# Seconds each of the check's commands, and the question to the second Mac, may take.
CHECK_TIMEOUT = 30
ASK_TIMEOUT = 60
# What Security says, in the words `security` and `codesign` print, when an operation needs a dialog
# the session cannot show (errSecInteractionNotAllowed, -25308), and when a keychain file is missing
# (errSecNoSuchKeychain, -25294).
INTERACTION = re.compile(r"User interaction is not allowed|-25308")
NO_SUCH_KEYCHAIN = re.compile(r"could not be found|-25294")
# find-identity's lines: `  1) SHA-1 "NAME"` for each identity and `N valid identities found`.
IDENTITY = re.compile(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.*)"\s*$', re.MULTILINE)
VALID = re.compile(r"(\d+) valid identit(?:y|ies) found")
# A copy of this executable is what the check signs: any Mac has it, and signing a copy changes nothing else.
PROBE = "/usr/bin/true"
PASSED, FAILED, UNMEASURED = "passed", "failed", "unmeasured"


def guarded(mac, command, elsewhere):
    """A shell command that runs command only on the Mac named mac, and anywhere else says which Mac
    it is on, prints elsewhere and fails. It works in zsh, bash and sh."""
    return (f'if [ "$(scutil --get LocalHostName)" = {shlex.quote(mac)} ]; then {command}; '
            f"else printf 'This is %s, not %s. %s\\n' \"$(scutil --get LocalHostName)\" {shlex.quote(mac)} "
            f"{shlex.quote(elsewhere)} >&2; false; fi")


def unlock_commands(here, location, path):
    """The owner's commands that unlock path on the target, labeled by the Mac each runs on: the
    local one on the target, and, when the target is the second Mac, the one that reaches it through
    the alias, for this Mac, which defines it. security asks for the password itself; neither command
    passes one."""
    unlock = shlex.join(["security", "unlock-keychain", path])
    target = location.mac
    local = guarded(target, unlock, f"This command is for a shell on {target}.")
    if location.where == "here":
        return [(target, local)]
    reach = shlex.join(["ssh", "-t", location.destination, local])
    remote_form = guarded(here, reach, f"{location.destination} is an alias of {here}'s ~/.ssh/config. In a shell already "
                                       f"on {target}, use the command for {target} instead.")
    return [(here, remote_form), (target, local)]


def check_commands(location, path, identity, sender=None):
    """The post-check's commands: in the automation's context on the second Mac, through eng remote
    from the Mac that sends work there, and in a shell on the target, which checks that shell's
    session only. sender names the Mac that sends work to the target when the target is this Mac."""
    check = ["python3", "scripts/eng", "keychain", "check", "--keychain", path,
             *(["--identity", identity] if identity else [])]
    through = shlex.join(["python3", "scripts/eng", "remote", "admit", "--", *check])
    lines = []
    if location.where == "second Mac":
        lines.append((f"from this Mac, as eng remote runs work on {location.mac}", through))
    elif sender:
        lines.append((f"from {sender}, as eng remote runs work on {location.mac}", through))
    lines.append((f"in a shell on {location.mac}, for that shell's session only", shlex.join(check)))
    return lines


def missing(info, path):
    """The refusal for a keychain file that is not there, with the files that are, closest first."""
    found = info.get("keychains") or []
    names = {Path(candidate).name: candidate for candidate in found}
    close = [names[name] for name in difflib.get_close_matches(Path(path).name, list(names), n=3, cutoff=0.6)]
    listed = close + [candidate for candidate in found if candidate not in close]
    text = f"{info.get('path') or path} is not on {info.get('mac')}: nothing there to unlock."
    if listed:
        text += " The keychain files there are:\n" + "\n".join(f"  {candidate}" for candidate in listed)
        text += ("\nRun this again with --keychain and one of them, copied rather than typed"
                 + (f", such as --keychain {shlex.quote(close[0])}" if close else ""))
    else:
        text += f" It has no keychain files in {info.get('home')}/{offload_host.KEYCHAINS}"
    return text


def recover(path=DEFAULT, target=None, identity=None, out=print, ask=None):
    """Say where the keychain the work needs is and print the owner's commands, or refuse: exit 0
    when it printed them, 2 when it refused. It unlocks nothing and reads no password. ask, given a
    destination and a request, asks the second Mac, as remote.ask does."""
    ask = ask or (lambda destination, request: remote.ask(destination, request, ASK_TIMEOUT))
    here, view, session = host_context.this_mac(), host_context.second_mac(), host_context.session()
    there = unreached = info = None
    if view["destination"] and not view["undefinedAlias"] and target != here \
            and not host_context.names_mac(view["hostname"], here):
        try:
            remote.reachable(view["destination"])
            info = ask(view["destination"], {"keychain": path})
        except remote.Unavailable as error:
            unreached = str(error)
        else:
            there = info.get("mac") if isinstance(info, dict) else None
            if there is None: unreached = f"it answered {info!r}, which names no Mac"
    location = host_context.locate(here, view, target, there, unreached)
    out(f"This Mac: {here}, in a session of kind {session['kind']}")
    if location.refusal:
        out(f"Refused: {location.refusal}. Nothing was unlocked.")
        return 2
    out(f"Target: {location.mac}" + (f" ({location.note})" if location.note else ""))
    if location.where == "here": info = offload_host.keychain(path)
    if not info.get("exists"):
        out(f"Refused: {missing(info, path)}. Nothing was unlocked.")
        return 2
    exact = info["path"]
    out(f"Keychain: {exact}, present on {location.mac}")
    out("Unlock it in your own Terminal, on the Mac each command names; security asks for the password there, and "
        "nothing else reads it. Each command refuses on any other Mac.")
    for mac, command in unlock_commands(here, location, exact):
        out(f"  On {mac}{', this Mac' if mac == here else ''}:\n    {command}")
    out("Then check where the automation runs; nothing there prompts, and an unlock elsewhere or an unlocked desktop "
        "does not count:")
    sender = view["deferredTo"] if location.where == "here" else None
    for where, command in check_commands(location, exact, identity, sender):
        out(f"  {where}:\n    {command}")
    return 0


def last_line(found):
    lines = [line.strip() for line in ((found.stderr or "") + "\n" + (found.stdout or "")).splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def attempt(run, command, timeout):
    """command's completed process, or None and why it gave no result: it never reads input."""
    try:
        return run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout), None
    except subprocess.TimeoutExpired:
        return None, f"{command[0]} {command[1]} did not finish in {timeout:g}s"
    except OSError as error:
        return None, f"{command[0]} could not run: {error}"


def keychain_status(found, failure=None):
    """unlocked, locked, missing or unmeasured, from `security show-keychain-info` run in a session
    that cannot show a dialog: unlocked when it read the keychain's settings, locked when Security
    refused it the interaction an unlock would need (errSecInteractionNotAllowed), and missing for
    errSecNoSuchKeychain. Anything else says nothing of the keychain."""
    if found is None: return {"status": UNMEASURED, "said": failure}
    said, text = last_line(found), (found.stdout or "") + (found.stderr or "")
    if found.returncode == 0: return {"status": "unlocked", "said": said}
    if INTERACTION.search(text): return {"status": "locked", "said": said}
    if NO_SUCH_KEYCHAIN.search(text): return {"status": "missing", "said": said}
    return {"status": UNMEASURED, "said": f"security exited {found.returncode}: {said}"}


def identity_presence(found, identity, failure=None):
    """Whether `security find-identity -v -p codesigning` lists a valid code-signing identity, the one
    named when identity is given: as codesign chooses one, by its SHA-1 or a part of its name. It keeps
    counts, not names or hashes."""
    if found is None: return {"status": UNMEASURED, "said": failure}
    text = (found.stdout or "") + (found.stderr or "")
    count = VALID.search(text)
    if found.returncode != 0 or count is None:
        return {"status": UNMEASURED, "said": f"security exited {found.returncode}: {last_line(found)}"}
    # With a policy, find-identity may list every matching identity before the valid ones.
    listed = IDENTITY.findall((found.stdout or "").rpartition("Valid identities only")[2])
    if identity is None:
        return {"status": "present" if int(count.group(1)) else "absent", "valid": int(count.group(1))}
    matching = [entry for entry in listed if entry[0].lower() == identity.lower() or identity in entry[1]]
    return {"status": "present" if matching else "absent", "valid": int(count.group(1)), "matching": len(matching)}


def signing(run, path, identity, timeout):
    """passed when codesign signed a copy of PROBE with identity from the keychain at path, which used
    the private key; failed when codesign refused; unmeasured when nothing was learned."""
    with tempfile.TemporaryDirectory(prefix="eng-keychain-") as scratch:
        probe = Path(scratch) / "probe"
        try:
            shutil.copyfile(PROBE, probe)
        except OSError as error:
            return {"status": UNMEASURED, "said": f"no copy of {PROBE} to sign: {error}"}
        found, failure = attempt(run, ["codesign", "--force", "--sign", identity, "--keychain", path,
                                       "--timestamp=none", str(probe)], timeout)
    if found is None: return {"status": UNMEASURED, "said": failure}
    if found.returncode: return {"status": FAILED, "said": f"codesign exited {found.returncode}: {last_line(found)}"}
    return {"status": PASSED}


def note(session):
    kind = session["kind"]
    if kind == "ssh":
        return ("an SSH login: no graphic access, so Security refuses interaction here rather than showing a dialog. "
                "These results hold for this kind of session, such as eng remote's runs; an unlock or a signing in a GUI "
                "session does not establish them, nor do they establish a GUI session's")
    if kind == "background":
        return "a session without graphic access, not flagged remote: nothing here can show a dialog"
    return (f"a session with graphic access ({kind}), where a keychain operation can show a dialog, so nothing that "
            "could raise one ran: run the check where the automation runs, such as through eng remote admit")


def check(path=DEFAULT, identity=None, timeout=CHECK_TIMEOUT, run=subprocess.run, session=None):
    """The post-check's record, in the session it runs in. outcome is passed only when signing
    passed, failed when a measured result rules key use out, and otherwise unmeasured."""
    session = session or host_context.session()
    record = {"mac": host_context.this_mac(), "session": {**session, "note": note(session)}, "keychain": {"path": path}}
    def unmeasured(cause):
        return {"status": UNMEASURED, "said": cause}
    if session.get("graphicAccess") is not False:
        cause = "not run: this session can show a keychain dialog" if session.get("graphicAccess") else \
            "not run: this session's flags could not be read, so it may show a keychain dialog"
        record["keychain"].update(unmeasured(cause))
        record["identity"] = record["signing"] = unmeasured(cause)
    else:
        info = offload_host.keychain(path)
        exact = record["keychain"]["path"] = info["path"]
        if not info["exists"]:
            record["keychain"].update(status="missing", said=f"no keychain file at {exact}")
            record["identity"] = record["signing"] = unmeasured("not run: the keychain file is missing")
        else:
            record["keychain"].update(keychain_status(*attempt(run, ["security", "show-keychain-info", exact], timeout)))
            found, failure = attempt(run, ["security", "find-identity", "-v", "-p", "codesigning", exact], timeout)
            record["identity"] = identity_presence(found, identity, failure)
            if identity is None:
                record["signing"] = unmeasured("not run: no --identity named, so the key's use is not measured")
            elif record["identity"]["status"] == "absent":
                record["signing"] = unmeasured("not run: the identity is not in the keychain")
            else:
                record["signing"] = signing(run, exact, identity, timeout)
    signed, kept, listed = record["signing"]["status"], record["keychain"]["status"], record["identity"]["status"]
    record["outcome"] = PASSED if signed == PASSED else \
        FAILED if signed == FAILED or kept == "missing" or listed == "absent" else UNMEASURED
    return record


def execute(args, out=print):
    """eng keychain recover or check."""
    if args.operation == "recover":
        return recover(args.keychain, args.target, args.identity, out)
    record = check(args.keychain, args.identity, args.timeout)
    out(json.dumps(record, indent=2))
    return {PASSED: 0, FAILED: 1}.get(record["outcome"], evidence.UNMEASURED)


def add_arguments(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    recover_parser = operations.add_parser("recover", help="say which Mac's keychain the work needs, check that the "
        "keychain file is there, and print the commands that unlock it, to copy into your own Terminal: each runs "
        "only on the Mac it names. It unlocks nothing and reads no password")
    recover_parser.add_argument("--target", metavar="MAC", help="the Mac whose keychain the work needs, by its "
        "LocalHostName (default: the second Mac, as eng remote reaches it from this Mac)")
    recover_parser.add_argument("--identity", help="the signing identity the check is to use; only printed")
    check_parser = operations.add_parser("check", help="where the automation runs, record the session, the "
        "keychain's status, whether the identity is there and whether signing with it succeeds, without anything "
        "that could show a dialog: exit 0 when signing passed, 1 when key use failed, 3 when unmeasured")
    check_parser.add_argument("--identity", help="the code-signing identity to look for and sign with, by a part of "
                              "its name or its SHA-1")
    check_parser.add_argument("--timeout", type=float, default=CHECK_TIMEOUT, help="seconds each command may take "
                              f"(default {CHECK_TIMEOUT})")
    for choice in (recover_parser, check_parser):
        choice.add_argument("--keychain", default=DEFAULT, help=f"the keychain file: a path, or a file name in "
                            f"~/Library/Keychains, never completed (default {DEFAULT})")
