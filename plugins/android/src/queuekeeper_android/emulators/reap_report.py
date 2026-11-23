"""Read the same Android reaping report on each Mac, where its reservations and emulators live."""
import functools
import json
import subprocess

from queuekeeper import farm, offload_host, remote
from .. import sdk


PROBE = """
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
request = json.loads(sys.argv[1])
checkout = (Path.home() / request["checkout"]).resolve()
sys.path.insert(0, str(checkout / "src/python"))
from queuekeeper import checkout as workspace
if Path(workspace.__file__).resolve() != checkout / "src/python/queuekeeper/checkout.py":
    raise SystemExit("the farm's Queuekeeper checkout could not be loaded")
workspace.activate(checkout)
from queuekeeper_android.emulators import reap_report
if not Path(reap_report.__file__).resolve().is_relative_to(checkout):
    raise SystemExit("the farm's Queuekeeper checkout lacks the Android reap report")
print(json.dumps(reap_report.receive(request["pool"])))
"""


def receive(name):
    """The farm probe's only entry point. It accepts a pool name and always runs the ownership checks in dry run."""
    from .pool import Pool

    return {"pool": name, "inventory": "verified", "results": Pool(name, None).reap(dry_run=True)}


def there(host, name):
    """Return the other Mac's report, or a reason it could not be checked."""
    request = json.dumps({"checkout": offload_host.TOOLKIT_CHECKOUT, "pool": name})
    try:
        done = remote.ssh(host, remote.host_command(PROBE, request), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=remote.CENSUS_TIMEOUT)
        if done.returncode:
            detail = (remote.last_line(done.stderr) or "no error output")[-300:]
            return f"probe exited {done.returncode}: {detail}"
        report = json.loads(remote.last_line(done.stdout) or "null")
    except subprocess.TimeoutExpired:
        return f"it did not answer in {remote.CENSUS_TIMEOUT:g}s"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return str(error)
    if isinstance(report, dict) and report.get("pool") == name and report.get("inventory") != "verified":
        return "the Android reap probe did not verify this Mac's inventory"
    rows = report.get("results") if isinstance(report, dict) and report.get("pool") == name else None
    if not isinstance(rows, list) or not all(isinstance(row, list) and len(row) == 2 and
                                            isinstance(row[0], str) and type(row[1]) is bool for row in rows):
        return "the Android reap probe did not return a report for this pool"
    return rows


def print_farm(pool):
    """Print this Mac's report and the active farm Macs' reports in registry order."""
    macs = farm.active()
    try:
        here = pool.reap(dry_run=True)
    except (OSError, ValueError, RuntimeError, sdk.Missing) as error:
        here = str(error) or type(error).__name__
    hosts = dict.fromkeys(mac["ssh"] for mac in macs)
    readings = remote.read_macs(hosts, functools.partial(there, name=pool.name), remote.CENSUS_TIMEOUT, name=str)
    failed = False
    for name, rows in [("this Mac", here), *((mac["name"], readings[mac["ssh"]]) for mac in macs)]:
        if isinstance(rows, str):
            print(f"{name}: unchecked: {rows}")
            failed = True
        elif not rows:
            print(f"{name}: {pool.name}: inventory verified; no recorded pool members to reap")
        else:
            for line, problem in rows:
                print(f"{name}: {line}")
                failed |= problem
    return int(failed)
