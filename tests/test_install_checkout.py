"""The setup script (tools/install-checkout.py), run as a process with a home, a user site and a PATH of the test's."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
import host_isolation  # noqa: F401 -- first, as every test module

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/install-checkout.py"


class Setup(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.local = self.home / ".local/bin"
        self.local.mkdir(parents=True)
        self.path = [self.local]

    def run_script(self, *arguments):
        environment = {"HOME": str(self.home), "PYTHONUSERBASE": str(self.home / "userbase"),
                       "PATH": os.pathsep.join([*map(str, self.path), "/usr/bin", "/bin"])}
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, env=environment,
                              timeout=host_isolation.stretched(60))

    def package(self):
        found = list((self.home / "userbase").rglob("queuekeeper.pth"))
        return found[0].read_text() if found else None

    def test_it_puts_qk_on_the_path_and_the_package_on_the_import_path(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.package(), f"{ROOT / 'src/python'}\n")
        command = self.local / "qk"
        self.assertTrue(os.access(command, os.X_OK))
        self.assertIn(f"`qk` now runs this checkout ({command})", done.stdout)
        self.assertNotIn("is not on the PATH", done.stdout)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_the_command_runs_this_checkout_with_the_python_that_installed_it_from_any_folder(self):
        self.run_script()
        # A PATH with no python3 at all: the command names its interpreter itself.
        ran = subprocess.run([str(self.local / "qk"), "--version"], capture_output=True, text=True, cwd=self.home,
                             env=host_isolation.reduced(HOME=str(self.home), PATH="/usr/bin:/bin"),
                             timeout=host_isolation.stretched(60))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertRegex(ran.stdout, r"\d+\.\d+")
        text = (self.local / "qk").read_text()
        self.assertIn(sys.executable, text)
        self.assertIn(str(ROOT / "bin/qk"), text)

    def test_the_first_writable_folder_on_the_path_is_used_and_one_that_is_not_on_it_is_said(self):
        other = self.home / "bin"
        other.mkdir()
        self.path = [other]
        done = self.run_script()
        self.assertTrue((other / "qk").is_file(), done.stdout)
        self.assertFalse((self.local / "qk").exists())
        shutil.rmtree(other)
        self.path = []
        done = self.run_script()
        self.assertEqual(done.returncode, 0)
        self.assertTrue((self.local / "qk").is_file())
        self.assertIn(f"{self.local} is not on the PATH", done.stdout)
        self.assertIn(f'export PATH="{self.local}:$PATH"', done.stdout)
        self.assertEqual(self.run_script("--check").returncode, 1)

    def test_a_folder_can_be_named(self):
        named = self.home / "tools"
        self.path.append(named)
        done = self.run_script("--bin", str(named))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((named / "qk").is_file())
        self.assertEqual(self.run_script("--check", "--bin", str(named)).returncode, 0)
        self.assertEqual(self.run_script("--check").returncode, 1)
        self.assertEqual(self.run_script("--remove", "--bin", str(named)).returncode, 0)
        self.assertFalse((named / "qk").exists())

    def test_a_qk_it_did_not_write_is_left_alone_and_reported(self):
        theirs = self.local / "qk"
        theirs.write_text("#!/bin/sh\necho another qk\n")
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("is a command this script did not write, so it is left as it is", done.stderr)
        self.assertEqual(theirs.read_text(), "#!/bin/sh\necho another qk\n")
        self.assertIsNotNone(self.package())  # the package is still made importable
        done = self.run_script("--remove")
        self.assertIn(f"left {theirs}, which this script did not write", done.stdout)
        self.assertTrue(theirs.exists())
        theirs.unlink()
        os.symlink("/bin/echo", theirs)
        self.assertEqual(self.run_script().returncode, 1)
        self.assertTrue(theirs.is_symlink())

    def test_its_own_command_is_replaced_checked_and_removed(self):
        self.run_script()
        command = self.local / "qk"
        command.write_text(command.read_text().replace(str(ROOT), "/another/checkout"))
        done = self.run_script("--check")
        self.assertEqual(done.returncode, 1)
        self.assertIn("runs something else", done.stdout)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script("--check").returncode, 0)
        done = self.run_script("--remove")
        self.assertEqual(done.returncode, 0)
        self.assertFalse(command.exists())
        self.assertIsNone(self.package())
        done = self.run_script("--check")
        self.assertEqual(done.returncode, 1)
        self.assertIn("missing", done.stdout)

    def test_a_mistaken_command_line_prints_the_usage(self):
        for arguments in (["--nope"], ["--check", "--remove"], ["--bin"]):
            done = self.run_script(*arguments)
            self.assertEqual(done.returncode, 2, arguments)
            self.assertIn("python3 tools/install-checkout.py --check", done.stderr)


if __name__ == "__main__":
    unittest.main()
