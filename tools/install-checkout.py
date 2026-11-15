#!/usr/bin/env python3
"""Set this checkout up for the Python that runs this script: put the `qk` command on the PATH, and make the
`queuekeeper` package importable by every program that interpreter runs, since a project's own scripts import
Queuekeeper's modules (`from queuekeeper import processes`).

    python3 tools/install-checkout.py              install both
    python3 tools/install-checkout.py --check      exit 0 when both name this checkout, 1 otherwise
    python3 tools/install-checkout.py --remove     remove both
    python3 tools/install-checkout.py --bin DIR    put the command in DIR instead; with --check and --remove too

The command is a two-line script, `qk`, that runs this checkout's bin/qk with the interpreter that ran this
script, whatever `python3` a shell finds first. It goes in the first folder of BIN that is on the PATH and can be
written, or in ~/.local/bin when none is, which the script then says to add to the PATH. A `qk` that this script
did not write is left alone and reported.

The packages are this checkout's core and plugin source roots, one per line in a .pth file in the interpreter's user site folder, as
`pip install --editable` would write. Run the script once on each Mac with the interpreter that runs Queuekeeper
there. A process that names another copy in QUEUEKEEPER_ROOT and runs it through scripts/qk still runs that copy,
since the launcher puts it first."""
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
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text("".join(f"{path}\n" for path in source_paths(CHECKOUT)))
    print(f"{sys.executable} now imports queuekeeper from {SOURCE} ({package})")
    if (command.exists() or command.is_symlink()) and not ours(command):
        print(f"queuekeeper: {command} is a command this script did not write, so it is left as it is: remove it, or "
              f"name another folder with --bin", file=sys.stderr)
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    staged = command.with_name(f".{COMMAND}.{os.getpid()}.tmp")
    staged.write_text(wanted())
    staged.chmod(0o755)
    os.replace(staged, command)
    print(f"`{COMMAND}` now runs this checkout ({command})")
    if not found:
        print(f"{folder} is not on the PATH, so a shell does not find `{COMMAND}` yet: add this line to ~/.zshrc and "
              f"open a new shell:\n    export PATH=\"{folder}:$PATH\"")
    return 0


def usage():
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
