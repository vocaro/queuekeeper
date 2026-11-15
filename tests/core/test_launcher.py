"""The project launcher and command started by an interpreter older than Queuekeeper runs under."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module
from host_isolation import stretched

ROOT = Path(__file__).resolve().parents[2]
OLD = "/usr/bin/python3"


def older():
    """Whether the Mac's system Python is one the launcher refuses."""
    if not os.access(OLD, os.X_OK): return False
    told = subprocess.run([OLD, "-c", "import sys; sys.exit(sys.version_info >= (3, 14))"], capture_output=True)
    return told.returncode == 0


@unittest.skipUnless(older(), f"{OLD} is not older than Python 3.14")
class OlderInterpreterTests(unittest.TestCase):
    launcher = "scripts/qk"

    def run_launcher(self, path):
        environment = {name: value for name, value in os.environ.items() if name != "QUEUEKEEPER_LAUNCHER_PYTHON"}
        return subprocess.run([OLD, str(ROOT / self.launcher), "--version"], env={**environment, "PATH": path},
                              capture_output=True, text=True, timeout=stretched(60))

    def test_it_hands_over_to_the_first_python3_on_the_path_that_is_new_enough(self):
        # As a project does that runs its lanes' launcher under a pinned environment's interpreter, whose folder
        # is first on the PATH: the older python3 there is passed over.
        result = self.run_launcher(os.pathsep.join(["/usr/bin", str(Path(sys.executable).parent), "/bin"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+\.\d+")

    def test_it_refuses_once_when_the_path_has_no_newer_python3(self):
        result = self.run_launcher("/usr/bin:/bin")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.count("needs Python 3.14 or newer"), 1, result.stderr)


class CommandOlderInterpreterTests(OlderInterpreterTests):
    """The command used by bare runners in projects without scripts/qk has the same interpreter contract."""
    launcher = "bin/qk"


if __name__ == "__main__":
    unittest.main()
