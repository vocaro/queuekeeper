from pathlib import Path
import sys
import unittest
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering.simulators.lifecycle import reserved
import host_isolation


class LifecycleTests(unittest.TestCase):
    def test_pool_adapter_never_shuts_down_or_deletes_its_device(self):
        calls = []
        device = reserved(str(uuid.uuid4()).upper(), run=lambda *args, **kw: calls.append(args))
        device.screenshot("result.png")
        self.assertEqual(calls, [("io", device.udid, "screenshot", "result.png")])
        with self.assertRaises(ValueError): reserved("booted")
