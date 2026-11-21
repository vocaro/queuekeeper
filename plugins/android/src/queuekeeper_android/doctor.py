"""What the Android plugin adds to `qk doctor`: tools and a JDK, and matching SDK packages across the farm."""
import json
from pathlib import Path
import subprocess

from queuekeeper import plugins, remote
from . import sdk


def warnings(root, config):
    """A warning for each missing piece a project that builds an Android app or keeps an emulator pool needs."""
    if "android" not in config and "emulatorPool" not in config: return []
    found = []
    for name in ("adb", "emulator", "avdmanager"):
        try: sdk.tool(name)
        except sdk.Missing as error: found.append(f"android: {error}")
    if sdk.java_home() is None:
        found.append(f"android: no JDK {sdk.JAVA_RELEASE} or later, which Gradle and avdmanager need: "
                     "`brew install --cask temurin@21`, or set JAVA_HOME")
    return found


def inventory_there(host):
    """The same SDK reading, run in another Mac's login shell without relying on its Queuekeeper checkout."""
    try:
        probe = Path(sdk.__file__).read_text() + "\nimport json\nprint(json.dumps(inventory()))\n"
        done = remote.ssh(host, remote.host_command(probe, "{}"), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=sdk.INVENTORY_TIMEOUT + 15)
        if done.returncode:
            detail = remote.last_line(done.stderr)
            return {"error": f"the SDK probe exited {done.returncode}" + (f": {detail[-300:]}" if detail else "")}
        report = json.loads(remote.last_line(done.stdout) or "")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"error": str(error)}
    if isinstance(report, dict) and (isinstance(report.get("packages"), dict) or isinstance(report.get("error"), str)):
        return report
    return {"error": "the SDK probe did not return an inventory"}


def compare_sdk(root, config, reports):
    """Attach each SDK reading to the farm report and fail doctor when installed package revisions differ."""
    if "android" not in config and "emulatorPool" not in config: return
    here = sdk.inventory()
    readings = remote.read_macs([report["host"] for report in reports if "remote" in report],
                               inventory_there, sdk.INVENTORY_TIMEOUT + 15, name=str)
    for report in reports:
        report.setdefault("local", {})["androidSdk"] = here
        if "error" in here:
            report["unchecked"].append(f"Android SDK packages on this Mac: {here['error']}")
        if "remote" not in report:
            continue  # The main comparison already says why that Mac did not answer.
        host = report["host"]
        there = readings[host]
        if isinstance(there, str): there = {"error": there}
        report["remote"]["androidSdk"] = there
        if "error" in there:
            report["unchecked"].append(f"Android SDK packages on {host}: {there['error']}")
        if "packages" not in here or "packages" not in there:
            continue
        ours, theirs = here["packages"], there["packages"]
        differences = [f"{name} is {ours.get(name, 'missing')} on this Mac and {theirs.get(name, 'missing')} on {host}"
                       for name in sorted(ours.keys() | theirs.keys()) if ours.get(name) != theirs.get(name)]
        if differences:
            report["errors"].append("Android SDK packages differ: " + ", ".join(differences) +
                                    ". Compare `sdkmanager --list_installed` on both Macs and align the revisions")


def register():
    if warnings not in plugins.DOCTOR: plugins.DOCTOR.append(warnings)
    if compare_sdk not in plugins.HOST_CHECKS: plugins.HOST_CHECKS.append(compare_sdk)
