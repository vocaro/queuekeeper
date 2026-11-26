#!/usr/bin/env python3
"""Set this checkout up for the Python that runs this script: put the `qk` command on the PATH, and make the
`queuekeeper` package importable by every program that interpreter runs, since a project's own scripts import
Queuekeeper's modules (`from queuekeeper import processes`). Installing also gives this Mac its CPU limit.

    python3 tools/install-checkout.py              install both, and set this Mac's CPU limit if it has none
    python3 tools/install-checkout.py --check      exit 0 when both name this checkout, 1 otherwise
    python3 tools/install-checkout.py --remove     remove both; the CPU limit stays
    python3 tools/install-checkout.py --bin DIR    put the command in DIR instead; with --check and --remove too

The command is a two-line script, `qk`, that runs this checkout's bin/qk with the interpreter that ran this
script, whatever `python3` a shell finds first. It goes in the first folder of BIN that is on the PATH and can be
written, or in ~/.local/bin when none is, which the script then says to add to the PATH. A `qk` that this script
did not write is left alone and reported.

The packages are this checkout's core and plugin source roots, one per line in a .pth file in the interpreter's user site folder, as
`pip install --editable` would write. Run the script once on each Mac with the interpreter that runs Queuekeeper
there. A process that names another copy in QUEUEKEEPER_ROOT and runs it through scripts/qk still runs that copy,
since the launcher puts it first.

The CPU limit is `busyLimit` in this Mac's admission limits file.
The script takes the Mac it runs on to be its owner's workstation and writes 70 there, below the default of 90 that
a Mac added to the farm with `qk farm add` keeps, so that this Mac is loaded less than the Macs that only take work.
A file that sets `busyLimit` already is left as it is, and so is one admission cannot read, which the script
reports. Run on a Mac that only takes work, the script writes 70 there too: set `busyLimit` in that Mac's file to
what it should be, before or after."""
import os
from pathlib import Path
import shlex
import site
import sys

if sys.version_info < (3, 14):
    print(f"queuekeeper: needs Python 3.14 or newer, and {sys.executable} is {sys.version.split()[0]}", file=sys.stderr)
    raise SystemExit(2)

CHECKOUT = Path(__file__).resolve().parents[1]
SOURCE = CHECKOUT / "src/python"
sys.path.insert(0, str(SOURCE))
from queuekeeper import resources
from queuekeeper.checkout import source_paths

COMMAND = "qk"
# Where the command may go, in order of preference: each is a folder shells commonly have on the PATH.
BIN = ("~/.local/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin")
# The line that marks a command as this script's, which alone it replaces or removes.
MARK = "# Queuekeeper's command, written by tools/install-checkout.py"


def on_path():
    return [Path(folder).expanduser() for folder in os.environ.get("PATH", "").split(os.pathsep) if folder]


def command_folder(given=None):
    """(the folder the command goes in, whether a shell finds it there): the one given, or the first of BIN that is
    on the PATH and can be written, or ~/.local/bin, which may have to be added to the PATH."""
    found = on_path()
    if given: return Path(given).expanduser().resolve(), Path(given).expanduser().resolve() in [f.resolve() for f in found]
    for folder in (Path(name).expanduser() for name in BIN):
        if folder in found and folder.is_dir() and os.access(folder, os.W_OK): return folder, True
    return Path(BIN[0]).expanduser(), Path(BIN[0]).expanduser() in found


def wanted():
    return (f"#!/bin/sh\n{MARK}: it runs this checkout\n# with the Python that installed it.\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(CHECKOUT / 'bin' / COMMAND))} \"$@\"\n")


def ours(path):
    """Whether path is a command this script wrote, for this checkout or another."""
    try: return not path.is_symlink() and MARK in path.read_text(errors="replace")
    except OSError: return False


def limit_set():
    """Give this Mac its CPU limit (resources.setup_limits) and say what was done, in words for someone who has not
    used Queuekeeper: (what to say, None), or (what to say, the error) when its limits file could not be read or
    written, which leaves the file as it is."""
    path = resources.limits_path()
    default = resources.LIMIT_KEYS["busyLimit"][2](1, 1)[0]
    try:
        wrote, limit = resources.setup_limits(path)
    except (ValueError, OSError) as error:
        # A file admission cannot read stops all its work on this Mac; one that cannot be written stops nothing.
        stops = (f" Queuekeeper runs nothing on this Mac until that file is fixed, or removed to use the default of "
                 f"{default:g}%." if isinstance(error, ValueError) else "")
        return f"CPU limit: not set, since {path} cannot be read or written.{stops} Then run this script again.", error
    if not wrote:
        return (f"CPU limit: left at {limit:g}%, which busyLimit in {path} sets already. To change it, edit it "
                f"there."), None
    return (f"CPU limit: Queuekeeper now runs one build or test at a time on this Mac once its CPU is {limit:g}% busy, "
            f"leaving room for your own work. A Mac you add later with `qk farm add` uses {default:g}%. To change it, "
            f"set busyLimit in {path}."), None


def limit_line():
    """This Mac's CPU limit in force and where it comes from, for --check, which it does not decide."""
    try:
        limit, source = resources.limits()["busyLimit"]
    except ValueError as error:
        return str(error)
    return f"{resources.limits_path()}: this Mac's CPU limit is {limit:g}% ({resources.limit_source(source, resources.limits_path())})"


def main(arguments):
    folder = None
    if "--bin" in arguments:
        at = arguments.index("--bin")
        if at + 1 >= len(arguments): return usage()
        folder, arguments = arguments[at + 1], arguments[:at] + arguments[at + 2:]
    if arguments not in ([], ["--check"], ["--remove"]): return usage()
    package = Path(site.getusersitepackages()) / "queuekeeper.pth"
    folder, found = command_folder(folder)
    command = folder / COMMAND
    imported = package.read_text().strip() if package.is_file() else None
    current = command.read_text(errors="replace") if command.is_file() and not command.is_symlink() else None
    if arguments == ["--check"]:
        right = imported == "\n".join(map(str, source_paths(CHECKOUT))), current == wanted() and os.access(command, os.X_OK)
        print(f"{package}: {imported or 'missing'}")
        print(f"{command}: {'runs this checkout' if right[1] else 'missing' if current is None else 'runs something else'}"
              + ("" if found else f", and {folder} is not on the PATH"))
        print(limit_line())
        return 0 if all(right) and found else 1
    if arguments == ["--remove"]:
        package.unlink(missing_ok=True)
        print(f"removed {package}")
        if ours(command):
            command.unlink()
            print(f"removed {command}")
        elif command.exists() or command.is_symlink():
            print(f"left {command}, which this script did not write")
        return 0
    # Every line about the install is said once it is done, so that its first line says whether it worked.
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text("".join(f"{path}\n" for path in source_paths(CHECKOUT)))
    imports = (f"Python package: {sys.executable} now imports the queuekeeper package from this checkout, for "
               f"projects whose own scripts use it. {package} names the folders.")
    limit, limit_error = limit_set()
    if (command.exists() or command.is_symlink()) and not ours(command):
        print(f"queuekeeper: installing failed: {command} is a command this script did not write, so it is left as it "
              f"is, and `{COMMAND}` does not run this checkout.\nRemove it, or name another folder for `{COMMAND}` "
              f"with --bin DIR, then run this script again.\n\nDone all the same:\n- {imports}\n- {limit}"
              + (f"\n\nqueuekeeper: this Mac's CPU limit is not set: {limit_error}" if limit_error else ""),
              file=sys.stderr)
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    staged = command.with_name(f".{COMMAND}.{os.getpid()}.tmp")
    staged.write_text(wanted())
    staged.chmod(0o755)
    os.replace(staged, command)
    runs = (f"`{COMMAND}` command: {command} now runs this checkout with {sys.executable}. Keep the checkout where it "
            f"is, since `{COMMAND}` runs from it; `git pull --ff-only` in it updates Queuekeeper.")
    next_step = (f"Next: run `{COMMAND} --help` to see its commands, then set up a project as \"Configure a project\" "
                 f"in {CHECKOUT / 'doc/user_guide.md'} describes.")
    if not found:
        runs += f" {folder} is not on the PATH yet, so a shell does not find `{COMMAND}` by name."
        next_step = (f"Next: add {folder} to the PATH by adding this line to ~/.zshrc, then open a new shell:\n"
                     f"    export PATH=\"{folder}:$PATH\"\nThen " + next_step[len("Next: "):])
    report = f"\nWhat this did:\n- {runs}\n- {imports}\n- {limit}\n\n{next_step}"
    if limit_error:
        print(f"queuekeeper: installed from {CHECKOUT}, but this Mac's CPU limit is not set: {limit_error}\n{report}",
              file=sys.stderr)
        return 1
    print(f"Queuekeeper is installed from {CHECKOUT}.\n{report}")
    return 0


def usage():
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
