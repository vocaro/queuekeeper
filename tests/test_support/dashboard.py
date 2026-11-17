"""The dashboard feature (queuekeeper_dashboard): its panels' data, its server and its command.

The page's scripts are not run here: test_the_page_and_every_panel_script_are_served checks that they are served,
and the page itself is checked in a browser."""
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from queuekeeper import cli, events, extensions, merge_queue as mq, visibility
from queuekeeper_dashboard import admission_ids, panels, registry, repository, server
from queuekeeper_dashboard.panels import farm, history, home, logs, queue

READ_DEVICES = farm.read_devices
READ_DETAIL = farm.read_detail  # the real one, which Fixture replaces so that no test asks a Mac
from test_support import queue as test_merge_queue

HOSTS = {"schemaVersion": 1, "hosts": [
    {"name": "this Mac (desk)", "role": "here", "state": "observed", "reasons": [], "limits": {"slots": 3, "slotsMax": 7},
     "slots": [{"slot": 0, "holder": "a"}, {"slot": 1, "holder": "b"}, {"slot": 2, "holder": None}],
     "holders": [{"identity": "holder-1", "label": "project gate", "heldSeconds": 90.0, "slots": [0], "wholeHost": True, "exclusive": False}],
     "waiters": [{"identity": "place-1", "request": "request-1", "label": "other unit", "waitedSeconds": 12.5, "reason": "every slot is busy"}]},
    {"name": "build", "role": "second", "state": "unknown", "reasons": ["ssh could not reach build"]}]}


LOADS = {"here": {"cpuBusy": 68.0, "busyLimit": 75.0, "load": 3.2, "memory": "normal", "screen": "locked", "error": None}}


class Fixture(test_merge_queue.Fixture):
    """A queue, a registry of the test's and a farm that is read from HOSTS and never from a Mac."""

    def setUp(self):
        super().setUp()
        listed = patch.object(registry, "path", lambda: self.base / "settings" / registry.FILE)
        listed.start()
        self.addCleanup(listed.stop)
        names = patch.object(admission_ids, "names_for", return_value={})
        names.start()
        self.addCleanup(names.stop)
        for name, value in (("read", lambda: HOSTS), ("read_detail", lambda: dict(LOADS)),
                            ("read_devices", lambda: {"devices": [], "deferredTo": None, "error": None})):
            reading = patch.object(farm, name, value)
            reading.start()
            self.addCleanup(reading.stop)
        self.addCleanup(farm.STATE.update, reading=None, detail={}, at=0.0, busy=False, error=None, samples=[])
        farm.STATE.update(reading=None, detail={}, at=0.0, busy=False, error=None, samples=[])
        self.one = repository.Repository("project", self.repo)

    def settle(self, branch, state=mq.FAILED, **fields):
        self.commit(f"{branch}.txt", branch)
        identifier, = self.add(branch)
        mq.Queue(self.repo).settle(self.state(identifier), state, **fields)
        self.one.state = None
        return identifier

    def read_farm(self):
        farm.current()
        deadline = time.monotonic() + host_isolation.stretched(10)
        while farm.STATE["busy"] and time.monotonic() < deadline: time.sleep(0.01)
