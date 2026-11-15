"""A simulator addressed by its canonical UDID, never by a name or `booted`.

Members of a project's pool are taken, created and released by queuekeeper_apple.simulators.pool;
this module only drives a device already held.
"""
import subprocess
import uuid


def command(*arguments, timeout=120):
    return subprocess.check_output(["xcrun", "simctl", *arguments], text=True, timeout=timeout).strip()


class Simulator:
    def __init__(self, udid, *, run=command):
        self.udid = str(uuid.UUID(udid)).upper()
        self.run = run

    def boot(self):
        self.run("boot", self.udid)
        self.run("bootstatus", self.udid, "-b", timeout=600)

    def install(self, application): self.run("install", self.udid, str(application))
    def launch(self, bundle, *arguments): return self.run("launch", self.udid, bundle, *arguments)
    def screenshot(self, path): self.run("io", self.udid, "screenshot", str(path))


def reserved(udid, *, run=command):
    """Use a device already reserved and handed off by project policy. No lifecycle mutation."""
    return Simulator(udid, run=run)
