"""A trial run: QUEUEKEEPER_TRIAL alone keeps doctor, run, land and queue off the account's real Queuekeeper state and
off every other Mac (vocaro/queuekeeper#34).

Each test takes away every namespace host_isolation names and sets the trial alone, so that what keeps it off the real
state is the switch. An audit hook, set for this process, refuses and records any file operation under the real
state's places and any ssh, scp or rsync, so that a switch that misses one fails the test without touching it."""
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))
import host_isolation  # noqa: F401 -- isolates this module's host state as every test module's
from queuekeeper import cli, plugins, farm, merge_queue as mq, offload_host, resources, trial

from test_support.queue import Fixture

shard_by = "class"


def real_places():
    """The account's real Queuekeeper state, each in both its spellings: what no trial may read or write."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    temporary = subprocess.check_output(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    places = [home / offload_host.ADMISSION_STATE, Path(temporary) / offload_host.SLOTS, Path(offload_host.GATE_LOCK),
              home / offload_host.PINS]
    found = set()
    for place in places:
        found.update({str(place), os.path.realpath(place)})
        if str(place).startswith(("/tmp/", "/var/")): found.add("/private" + str(place))
    return tuple(sorted(found))


REMOTE = {"ssh", "scp", "rsync"}
# The audit hook's watch: the places to refuse, while a test sets them, and what it refused.
WATCH = {"places": (), "refused": []}


def audit(event, arguments):
    if not WATCH["places"]: return
    if event == "subprocess.Popen":
        command = arguments[1] if isinstance(arguments[1], (list, tuple)) else [arguments[1] or arguments[0]]
        if command and os.path.basename(str(command[0])) in REMOTE:
            WATCH["refused"].append(f"{event} {command}")
            raise PermissionError(f"a trial ran {command}")
        return
    if event not in ("open", "os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.listdir",
                     "os.scandir", "shutil.rmtree", "os.chmod", "os.symlink", "os.link", "os.truncate"):
        return
    for argument in arguments[:2]:
        if not isinstance(argument, (str, bytes, os.PathLike)): continue
        path = os.fsdecode(argument)
        if any(path == place or path.startswith(place.rstrip("/") + "/") for place in WATCH["places"]):
            WATCH["refused"].append(f"{event} {path}")
            raise PermissionError(f"a trial reached the real state at {path}")


sys.addaudithook(audit)


class Trial(Fixture):
    def setUp(self):
        super().setUp()
        self.trial = self.base / "trial"
        # The trial alone: every namespace host_isolation set is taken away, and the module defaults put back.
        alone = {name: value for name, value in os.environ.items() if name not in host_isolation.ISOLATION
                 and name not in trial.PLACES}
        environment = patch.dict(os.environ, {**alone, trial.VARIABLE: str(self.trial)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for module in (resources, offload_host):
            restored = patch.object(module, "GATE_LOCK", "/tmp/queuekeeper-gate-suite.lock")
            restored.start()
            self.addCleanup(restored.stop)
        # A second Mac in the trial's farm, which no trial may compare, fix or send work to.
        (self.trial / trial.PLACES["HEAVY_SLOT_DIR"]).mkdir(parents=True)
        (self.trial / trial.PLACES["HEAVY_SLOT_DIR"] / farm.FILE).write_text(json.dumps({"version": farm.VERSION,
            "macs": [{"id": "AAAA-1", "name": "Studio", "ssh": "studio.invalid", "state": farm.ACTIVE}]}))
        WATCH.update(places=real_places(), refused=[])
        self.addCleanup(WATCH.update, places=(), refused=[])

    def main(self, *words):
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.redirect_stderr(io.StringIO()) as told:
            status = cli.main(["--project", str(self.repo), *words])
        self.assertEqual(WATCH["refused"], [])
        self.told = told.getvalue()
        return status, said.getvalue()

    def test_the_switch_points_every_namespace_at_the_trial(self):
        found = {}
        self.assertEqual(trial.apply(found), None)
        self.assertEqual(found, {})
        self.assertEqual(trial.apply(found := {trial.VARIABLE: str(self.trial)}), self.trial)
        for name, place in trial.PLACES.items(): self.assertEqual(found[name], str(self.trial / place))
        for name in trial.OFF: self.assertEqual(found[name], "off")
        with self.assertRaisesRegex(ValueError, "not an absolute path"): trial.apply({trial.VARIABLE: "trial"})

    def test_doctor_compares_no_second_mac_and_fixes_nothing(self):
        fixed = []
        with patch.object(cli, "fix_hosts", host_isolation.FIX_HOSTS), \
                patch.object(plugins, "HOST_FIXES", [lambda root, hosts: fixed.append(hosts) or hosts]), \
                patch.object(cli.shutil, "which", return_value="/usr/bin/true"), \
                patch.object(cli, "source_identity", return_value={}):
            status, said = self.main("doctor")
        report = json.loads(said)
        self.assertEqual(fixed, [])
        self.assertEqual([hosts.get("host") for hosts in report["farm"]], [None])
        self.assertIn(f"{trial.refusal('the fixes')}; its state is under {self.trial}", report["warnings"])
        self.assertIn(status, (0, 1))

    def test_a_run_takes_its_admission_in_the_trial(self):
        status, _ = self.main("run", "gate")
        self.assertEqual(status, 0, self.told)
        self.assertTrue((self.trial / trial.PLACES["HEAVY_SLOT_DIR"] / resources.LOG).is_file())

    def test_land_and_queue_keep_to_the_trial(self):
        self.commit("a", "a")
        with patch.object(mq.Queue, "start"), contextlib.chdir(self.repo):
            status, _ = self.main("land", "a")
        self.assertEqual(status, 0, self.told)
        self.assertEqual(self.work(), 0)
        self.assertEqual(WATCH["refused"], [])
        self.assertIn("a", self.main_files())
        status, _ = self.main("queue", "status")
        self.assertEqual(status, 0, self.told)

    def test_qk_farm_changes_no_mac(self):
        with contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(farm.main(["add", "studio.invalid"]), 2)
        self.assertIn(trial.refusal("qk farm add"), said.getvalue())
        self.assertEqual(farm.read(), [])


if __name__ == "__main__":
    unittest.main()
