#!/usr/bin/env python3
"""Make this checkout's `queuekeeper` package importable by the Python that runs this script, for every program
that interpreter runs: a project's own scripts import Queuekeeper's modules (`from queuekeeper import processes`).

    python3 tools/install-checkout.py            write queuekeeper.pth in the interpreter's user site folder
    python3 tools/install-checkout.py --check    exit 0 when that file names this checkout, 1 otherwise
    python3 tools/install-checkout.py --remove   delete it

It writes one line, this checkout's src/python, in a .pth file, as `pip install --editable` would. Run it once
on each Mac with the interpreter that runs Queuekeeper there. A process that names another copy in
QUEUEKEEPER_ROOT and runs it through scripts/qk still runs that copy, since the launcher puts it first."""
from pathlib import Path
import site
import sys

if sys.version_info < (3, 14):
    print(f"queuekeeper: needs Python 3.14 or newer, and {sys.executable} is {sys.version.split()[0]}", file=sys.stderr)
    raise SystemExit(2)
source = Path(__file__).resolve().parents[1] / "src/python"
target = Path(site.getusersitepackages()) / "queuekeeper.pth"
arguments = sys.argv[1:]
if arguments not in ([], ["--check"], ["--remove"]):
    print(__doc__, file=sys.stderr)
    raise SystemExit(2)
current = target.read_text().strip() if target.is_file() else None
if arguments == ["--check"]:
    print(f"{target}: {current or 'missing'}")
    raise SystemExit(0 if current == str(source) else 1)
if arguments == ["--remove"]:
    target.unlink(missing_ok=True)
    print(f"removed {target}")
    raise SystemExit(0)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(f"{source}\n")
print(f"{sys.executable} now imports queuekeeper from {source} ({target})")
