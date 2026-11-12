"""The environment that puts the build-tool wrappers first on a lane's PATH.

This module does not import `host_isolation` yet, as every test module does: that helper does not
import until `cli` is split (vocaro/vocaro-engineering#403). It reads no host state."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from queuekeeper import shims


class Environment(unittest.TestCase):
    def test_the_wrappers_come_first_once_with_the_root(self):
        once = shims.environment({"PATH": "/usr/bin:/bin", "OTHER": "1"}, "/tmp")
        self.assertEqual(once["PATH"].split(os.pathsep), [str(shims.SHIMS), "/usr/bin", "/bin"])
        self.assertEqual((once["OTHER"], once[shims.ROOT_VARIABLE]), ("1", str(Path("/tmp").resolve())))
        # A nested lane's own root replaces its caller's, and the wrappers stay first, once.
        twice = shims.environment({**once, "PATH": "/opt/bin:" + once["PATH"]}, "/var")
        self.assertEqual(twice["PATH"].split(os.pathsep), [str(shims.SHIMS), "/opt/bin", "/usr/bin", "/bin"])
        self.assertEqual(twice[shims.ROOT_VARIABLE], str(Path("/var").resolve()))


if __name__ == "__main__":
    unittest.main()
