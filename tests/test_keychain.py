from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/python"))
from vocaro_engineering import cli, evidence, host_context, keychain, offload_host, remote

# Invented Mac names: the workstation that sends work, the second Mac, and a third Mac neither knows.
WORKSTATION, SECOND, THIRD = "workstation-mac", "second-mac-host", "third-mac"
HOME = "/Users/someone"
LOGIN = f"{HOME}/Library/Keychains/login.keychain-db"
# A code-signing identity as find-identity lists it: an invented SHA-1 and name.
SHA = "0123456789ABCDEF0123456789ABCDEF01234567"
NAME = "Apple Development: Someone (TEAM123456)"
LISTED = f'  1) {SHA} "{NAME}"\n     1 valid identities found\n'
# The audit session flags of a GUI login, as this Mac's Terminal and app sessions have them, and an SSH login's.
GUI_FLAGS = host_context.GRAPHIC | host_context.TTY | host_context.CONSOLE | host_context.AUTHENTICATED
SSH_FLAGS = host_context.REMOTE | host_context.TTY | host_context.AUTHENTICATED
GUI = host_context.session(GUI_FLAGS, {})
SSH = host_context.session(SSH_FLAGS, {"SSH_CONNECTION": "192.0.2.1 50000 192.0.2.2 22"})

# Stand-ins for the commands the printed commands run: scutil names the Mac FAKE_MAC says, security
# and ssh write their arguments, one per line, to the log, and ssh runs its remote command as that
# Mac's login shell would, on the Mac FAKE_REMOTE_MAC names.
FAKE_SCUTIL = '#!/bin/sh\n[ "$1 $2" = "--get LocalHostName" ] || exit 1\nprintf "%s\\n" "$FAKE_MAC"\n'
FAKE_SECURITY = '#!/bin/sh\nprintf "security\\n" >> "$FAKE_LOG"\nfor a in "$@"; do printf "%s\\n" "$a" >> "$FAKE_LOG"; done\n' \
                '[ -t 0 ] && printf "tty\\n" >> "$FAKE_LOG"\nexit 0\n'
FAKE_SSH = '#!/bin/sh\nprintf "ssh\\n" >> "$FAKE_LOG"\nfor a in "$@"; do printf "%s\\n" "$a" >> "$FAKE_LOG"; done\n' \
           'for last in "$@"; do :; done\nFAKE_MAC="$FAKE_REMOTE_MAC" exec /bin/sh -c "$last"\n'


def view(destination=None, hostname=None, undefined=False, deferred=None):
    return {"destination": destination, "hostname": hostname, "undefinedAlias": undefined, "deferredTo": deferred}


WORKSTATION_VIEW = view("build-host", f"{SECOND}.local")
SECOND_VIEW = view(deferred=WORKSTATION)


def completed(command, status=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, status, stdout, stderr)


class Commands:
    """A stand-in for subprocess.run that answers each command by its first two words, records every
    call with its options, and fails a test that gives one input or no time limit."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, command, **options):
        self.calls.append((command, options))
        assert options.get("stdin") is subprocess.DEVNULL and options.get("timeout"), options
        answer = self.answers[f"{command[0]} {command[1]}".replace("-", "_").replace(" ", "__")]
        if isinstance(answer, BaseException): raise answer
        return completed(command, *answer)


class SessionTests(unittest.TestCase):
    def test_the_audit_flags_say_which_session_runs_the_command(self):
        self.assertEqual((GUI["kind"], GUI["graphicAccess"], GUI["remote"], GUI["sshVariables"]), ("gui", True, False, False))
        self.assertEqual((SSH["kind"], SSH["graphicAccess"], SSH["remote"], SSH["sshVariables"]), ("ssh", False, True, True))
        self.assertIn("remote", SSH["flags"])
        background = host_context.session(host_context.AUTHENTICATED, {})
        self.assertEqual((background["kind"], background["graphicAccess"]), ("background", False))
        # Unreadable flags are unknown, never taken as a session that cannot show a dialog.
        unknown = host_context.session(None, {"SSH_TTY": "/dev/ttys001"})
        self.assertEqual((unknown["kind"], unknown["graphicAccess"], unknown["sshVariables"]), ("unknown", None, True))
        # A process started from an SSH shell inherits its variables in whatever session it then runs in.
        inherited = host_context.session(GUI_FLAGS, {"SSH_CONNECTION": "x"})
        self.assertEqual((inherited["kind"], inherited["sshVariables"]), ("gui", True))

    def test_this_process_s_audit_session_flags_are_read(self):
        flags = host_context.audit_flags()
        self.assertIsInstance(flags, int)
        self.assertIn(host_context.session(flags, {})["kind"], {"gui", "ssh", "background"})
        with patch.object(host_context.ctypes, "CDLL", side_effect=OSError("no libc")):
            self.assertIsNone(host_context.audit_flags())
            self.assertEqual(host_context.session(environment={})["kind"], "unknown")

    def test_the_second_mac_is_the_canonical_resolver_s_and_an_undefined_alias_is_named(self):
        cases = [
            # The workstation: the alias its ~/.ssh/config defines resolves to the second Mac's name.
            ("build-host", {"hostname": f"{SECOND}.local"}, {}, view("build-host", f"{SECOND}.local")),
            # A shell on the second Mac that names the workstation's alias: ssh -G gives it back unchanged.
            ("build-host", {"hostname": "build-host"}, {"deferredTo": WORKSTATION},
             view("build-host", "build-host", True, WORKSTATION)),
            # A destination that is a host name of its own is no undefined alias.
            ("mac.example", {"hostname": "mac.example"}, {}, view("mac.example", "mac.example")),
            (None, {}, {"deferredTo": WORKSTATION}, view(deferred=WORKSTATION)),
        ]
        for destination, settings, record, expected in cases:
            with self.subTest(destination=destination, record=record), \
                    patch.object(remote, "configured", return_value=destination), \
                    patch.object(remote, "ssh_settings", return_value=settings), \
                    patch.object(offload_host, "device_record", return_value=record):
                self.assertEqual(host_context.second_mac(), expected)


class LocateTests(unittest.TestCase):
    def test_from_the_workstation_the_target_is_the_second_mac_its_alias_reaches(self):
        found = host_context.locate(WORKSTATION, WORKSTATION_VIEW, there=SECOND)
        self.assertEqual((found.where, found.mac, found.destination, found.refusal), ("second Mac", SECOND, "build-host", None))
        named = host_context.locate(WORKSTATION, WORKSTATION_VIEW, SECOND, there=SECOND)
        self.assertEqual((named.where, named.mac), ("second Mac", SECOND))
        # The workstation's own keychain is the target only when the owner names this Mac.
        own = host_context.locate(WORKSTATION, WORKSTATION_VIEW, WORKSTATION)
        self.assertEqual((own.where, own.mac, own.destination), ("here", WORKSTATION, None))

    def test_on_the_second_mac_without_the_alias_the_target_is_here(self):
        found = host_context.locate(SECOND, SECOND_VIEW)
        self.assertEqual((found.where, found.mac, found.refusal), ("here", SECOND, None))
        self.assertIn("already on the target", found.note)
        self.assertEqual(host_context.locate(SECOND, SECOND_VIEW, SECOND).where, "here")
        # An alias that resolves to this Mac itself names this Mac too.
        self.assertEqual(host_context.locate(SECOND, view("build-host", f"{SECOND.upper()}.local")).where, "here")
        self.assertEqual(host_context.locate(SECOND, WORKSTATION_VIEW, there=SECOND).where, "here")

    def test_a_wrong_host_or_an_alias_this_mac_does_not_define_is_refused_with_what_to_do(self):
        cases = [
            # The owner's shell is on the second Mac and names the workstation's alias.
            (SECOND, view("build-host", "build-host", True, WORKSTATION), None, None, ["ssh -G build-host", f"--target {SECOND}"]),
            # On the second Mac, asked for the workstation's keychain: no alias here reaches it.
            (SECOND, SECOND_VIEW, WORKSTATION, None, [f"run this on {WORKSTATION}", "does not resolve here"]),
            (SECOND, SECOND_VIEW, THIRD, None, [f"run this on {THIRD}"]),
            # On the workstation, asked for a Mac that is neither it nor the second Mac.
            (WORKSTATION, WORKSTATION_VIEW, THIRD, SECOND, [f"{THIRD} is neither this Mac", f"run this on {THIRD}"]),
            # A Mac that names no second Mac and is none.
            (THIRD, view(), None, None, ["names no second Mac", f"--target {THIRD}"]),
        ]
        for here, seen, target, there, said in cases:
            with self.subTest(here=here, target=target):
                found = host_context.locate(here, seen, target, there)
                self.assertIsNone(found.where)
                for words in said: self.assertIn(words, found.refusal)
        # A Mac that names no second Mac may still be the target when the owner names it.
        self.assertEqual(host_context.locate(THIRD, view(), THIRD).where, "here")

    def test_a_second_mac_that_could_not_be_reached_is_refused_with_why(self):
        found = host_context.locate(WORKSTATION, WORKSTATION_VIEW, unreached="build-host cannot be reached: timed out")
        self.assertIsNone(found.where)
        self.assertIn("timed out", found.refusal)


class KeychainFileTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.home)]))
        folder = self.home / offload_host.KEYCHAINS
        folder.mkdir(parents=True)
        for name in ["login.keychain-db", "build.keychain"]: (folder / name).write_bytes(b"kych")
        (folder / "metadata.keychain-db").mkdir()
        (folder / "notes.txt").write_text("not a keychain\n")

    def test_the_exact_keychain_file_is_found_and_never_completed(self):
        login = str(self.home / offload_host.KEYCHAINS / "login.keychain-db")
        with patch.dict(os.environ, {"HOME": str(self.home)}), patch.object(offload_host, "mac_name", return_value=SECOND):
            for given in [keychain.DEFAULT, "login.keychain-db", login, "Library/Keychains/login.keychain-db"]:
                with self.subTest(given):
                    found = offload_host.keychain(given)
                    self.assertEqual((found["mac"], found["path"], found["exists"]), (SECOND, login, True))
            # Keychain files only: not a directory with a keychain's name, and not other files.
            self.assertEqual([Path(path).name for path in found["keychains"]], ["build.keychain", "login.keychain-db"])
            for misspelled in ["login.keychain-d", "login", "~/Library/Keychains/login.keychain"]:
                with self.subTest(misspelled):
                    self.assertFalse(offload_host.keychain(misspelled)["exists"])

    def test_the_second_mac_answers_the_question_with_its_own_python(self):
        # As remote.ask sends it: offload_host.py's source run by python3, the request as its argument.
        found = subprocess.run([sys.executable, "-c", Path(offload_host.__file__).read_text(),
                                json.dumps({"keychain": keychain.DEFAULT})], capture_output=True, text=True,
                               env={**os.environ, "HOME": str(self.home)}, timeout=60)
        self.assertEqual(found.returncode, 0, found.stderr)
        answer = json.loads(found.stdout)
        self.assertEqual((answer["exists"], answer["home"]), (True, str(self.home)))
        self.assertTrue(answer["mac"])


class CommandTests(unittest.TestCase):
    """The printed commands run in a shell, with stand-ins for scutil, security and ssh."""

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.bin)]))
        for name, text in [("scutil", FAKE_SCUTIL), ("security", FAKE_SECURITY), ("ssh", FAKE_SSH)]:
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.log = self.bin / "log"

    def run_on(self, mac, command, shell=("/bin/sh",), remote_mac=None):
        self.log.write_text("")
        found = subprocess.run([*shell, "-c", command], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                               env={"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_MAC": mac, "FAKE_LOG": str(self.log),
                                    "FAKE_REMOTE_MAC": remote_mac or ""}, timeout=60)
        return found, self.log.read_text().splitlines()

    def commands(self, here, where, path=LOGIN):
        location = host_context.locate(here, WORKSTATION_VIEW if where == "second Mac" else SECOND_VIEW,
                                       there=SECOND if where == "second Mac" else None)
        return dict(keychain.unlock_commands(here, location, path))

    def test_the_stand_ins_see_the_unguarded_commands_reach_the_wrong_mac(self):
        # The control: the commands #65's owner was given run wherever they are pasted.
        found, called = self.run_on(WORKSTATION, shlex.join(["security", "unlock-keychain", LOGIN]))
        self.assertEqual((found.returncode, called), (0, ["security", "unlock-keychain", LOGIN]))
        found, called = self.run_on(SECOND, shlex.join(["ssh", "build-host", "security unlock-keychain " + LOGIN]),
                                    remote_mac=SECOND)
        self.assertEqual(called[:2], ["ssh", "build-host"])

    def test_the_local_command_unlocks_only_on_its_own_mac_and_passes_no_password(self):
        local = self.commands(SECOND, "here")[SECOND]
        # zsh, the owner's login shell, without the rc files of whoever runs the test.
        for shell in [("/bin/sh",), ("/bin/zsh", "-f")]:
            with self.subTest(shell):
                found, called = self.run_on(SECOND, local, shell)
                self.assertEqual(found.returncode, 0, found.stderr)
                self.assertEqual(called, ["security", "unlock-keychain", LOGIN])
                # On the workstation it says where it is and unlocks nothing.
                found, called = self.run_on(WORKSTATION, local, shell)
                self.assertNotEqual(found.returncode, 0)
                self.assertEqual(called, [])
                self.assertIn(f"This is {WORKSTATION}, not {SECOND}.", found.stderr)

    def test_the_alias_command_runs_only_on_the_mac_that_defines_the_alias(self):
        commands = self.commands(WORKSTATION, "second Mac")
        self.assertEqual(list(commands), [WORKSTATION, SECOND])
        found, called = self.run_on(WORKSTATION, commands[WORKSTATION], remote_mac=SECOND)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(called[:3], ["ssh", "-t", "build-host"])
        self.assertEqual(called[4:], ["security", "unlock-keychain", LOGIN])
        # Pasted into a shell already on the second Mac, it reaches for no alias and says what to use.
        found, called = self.run_on(SECOND, commands[WORKSTATION])
        self.assertNotEqual(found.returncode, 0)
        self.assertEqual(called, [])
        self.assertIn(f"build-host is an alias of {WORKSTATION}'s ~/.ssh/config", found.stderr)
        self.assertIn(f"use the command for {SECOND} instead", found.stderr)
        # An alias that reaches another Mac than the one checked unlocks nothing there.
        found, called = self.run_on(WORKSTATION, commands[WORKSTATION], remote_mac=THIRD)
        self.assertNotEqual(found.returncode, 0)
        self.assertEqual(called[:3], ["ssh", "-t", "build-host"])
        self.assertNotIn("security", called)
        self.assertIn(f"This is {THIRD}, not {SECOND}.", found.stderr)
        # The second Mac's own command, run there, is the local one.
        found, called = self.run_on(SECOND, commands[SECOND])
        self.assertEqual(called, ["security", "unlock-keychain", LOGIN])

    def test_a_path_that_needs_quoting_reaches_security_exactly(self):
        odd = f"{HOME}/Library/Keychains/it's a keychain $HOME.keychain-db"
        for here, where in [(SECOND, "here"), (WORKSTATION, "second Mac")]:
            with self.subTest(where):
                found, called = self.run_on(here, self.commands(here, where, odd)[here], remote_mac=SECOND)
                self.assertEqual(found.returncode, 0, found.stderr)
                self.assertEqual(called[-2:], ["unlock-keychain", odd])

    def test_no_command_gives_security_a_password(self):
        for here, where in [(SECOND, "here"), (WORKSTATION, "second Mac")]:
            for command in self.commands(here, where).values():
                words = shlex.split(command)
                self.assertNotIn("-p", words)
                self.assertNotIn("set-key-partition-list", command)
                self.assertNotIn("|", words)


class RecoverTests(unittest.TestCase):
    def recover(self, here, seen, target=None, path=keychain.DEFAULT, answer=None, reachable=None, local=None,
                session=GUI, identity=None):
        lines, asked = [], []

        def ask(destination, request):
            asked.append((destination, request))
            return answer
        with patch.object(host_context, "this_mac", return_value=here), \
                patch.object(host_context, "second_mac", return_value=seen), \
                patch.object(host_context, "session", return_value=session), \
                patch.object(remote, "reachable", side_effect=reachable) as checked, \
                patch.object(offload_host, "keychain", return_value=local) as read_here, \
                patch("builtins.input", side_effect=AssertionError("input read")), \
                patch("getpass.getpass", side_effect=AssertionError("password read")):
            status = keychain.recover(path, target, identity, lines.append, ask)
        return status, "\n".join(lines), asked, checked.call_count, read_here.call_count

    def present(self, mac, path=LOGIN):
        return {"mac": mac, "home": HOME, "path": path, "exists": True, "keychains": [path]}

    def test_from_the_workstation_it_checks_the_second_mac_s_keychain_and_prints_both_commands(self):
        status, said, asked, checked, read_here = self.recover(
            WORKSTATION, WORKSTATION_VIEW, answer=self.present(SECOND), identity=NAME)
        self.assertEqual(status, 0, said)
        self.assertEqual((asked, checked, read_here), ([("build-host", {"keychain": keychain.DEFAULT})], 1, 0))
        for words in [f"This Mac: {WORKSTATION}", f"Target: {SECOND}", f"Keychain: {LOGIN}, present on {SECOND}",
                      f"On {WORKSTATION}, this Mac:", f"On {SECOND}:", "ssh -t build-host",
                      shlex.join(["python3", "scripts/eng", "remote", "admit", "--", "python3", "scripts/eng", "keychain",
                                  "check", "--keychain", LOGIN, "--identity", NAME])]:
            self.assertIn(words, said)

    def test_on_the_second_mac_it_gives_the_local_command_and_never_the_alias(self):
        status, said, asked, checked, read_here = self.recover(SECOND, SECOND_VIEW, local=self.present(SECOND),
                                                               session=SSH)
        self.assertEqual(status, 0, said)
        self.assertEqual((asked, checked, read_here), ([], 0, 1))
        self.assertIn("already on the target", said)
        self.assertIn(f"This Mac: {SECOND}, in a session of kind ssh", said)
        self.assertNotIn("ssh -t", said)
        self.assertNotIn("build-host", said)
        self.assertIn(f"from {WORKSTATION}, as eng remote runs work on {SECOND}", said)

    def test_a_wrong_host_or_an_alias_this_mac_does_not_define_is_refused_before_anything_is_asked(self):
        for here, seen, target in [(SECOND, view("build-host", "build-host", True, WORKSTATION), None),
                                   (SECOND, SECOND_VIEW, WORKSTATION), (THIRD, view(), None)]:
            with self.subTest(here=here, target=target):
                status, said, asked, checked, read_here = self.recover(here, seen, target, local=self.present(here))
                self.assertEqual((status, asked, checked, read_here), (2, [], 0, 0))
                self.assertIn("Refused:", said)
                self.assertNotIn("unlock-keychain", said)
        status, said, *_ = self.recover(WORKSTATION, WORKSTATION_VIEW, THIRD, answer=self.present(SECOND))
        self.assertEqual(status, 2)
        self.assertNotIn("unlock-keychain", said)

    def test_a_misspelled_or_missing_keychain_is_refused_with_the_files_that_are_there(self):
        folder = f"{HOME}/Library/Keychains"
        answer = {"mac": SECOND, "home": HOME, "path": f"{folder}/login.keychain-d", "exists": False,
                  "keychains": [f"{folder}/build.keychain", f"{folder}/login.keychain-db"]}
        status, said, *_ = self.recover(WORKSTATION, WORKSTATION_VIEW, path="~/Library/Keychains/login.keychain-d",
                                        answer=answer)
        self.assertEqual(status, 2, said)
        self.assertIn(f"{folder}/login.keychain-d is not on {SECOND}", said)
        self.assertIn(f"such as --keychain {LOGIN}", said)
        self.assertLess(said.index(f"  {LOGIN}"), said.index(f"  {folder}/build.keychain"))
        self.assertNotIn("unlock-keychain", said)
        empty = {**answer, "keychains": []}
        status, said, *_ = self.recover(SECOND, SECOND_VIEW, local=empty)
        self.assertEqual(status, 2)
        self.assertIn("It has no keychain files", said)

    def test_a_second_mac_that_cannot_be_reached_is_refused_with_why(self):
        offline = remote.Unreachable("build-host cannot be reached: ssh: Could not resolve hostname", "wake it")
        status, said, asked, *_ = self.recover(WORKSTATION, WORKSTATION_VIEW, reachable=offline)
        self.assertEqual((status, asked), (2, []))
        self.assertIn("Could not resolve hostname", said)
        status, said, *_ = self.recover(WORKSTATION, WORKSTATION_VIEW, answer={"error": "no"})
        self.assertEqual(status, 2)
        self.assertIn("names no Mac", said)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.home)]))
        (self.home / offload_host.KEYCHAINS).mkdir(parents=True)
        self.login = self.home / offload_host.KEYCHAINS / "login.keychain-db"
        self.login.write_bytes(b"kych")
        environment = patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        named = patch.object(host_context, "this_mac", return_value=SECOND)
        named.start()
        self.addCleanup(named.stop)

    def commands(self, keychain_info=(0, f'Keychain "{LOGIN}" lock-on-sleep timeout=300s\n'), identity=(0, LISTED),
                 sign=(0,)):
        return Commands(security__show_keychain_info=keychain_info, security__find_identity=identity,
                        codesign____force=sign)

    def test_in_a_session_that_can_show_a_dialog_nothing_runs_and_every_dimension_is_unmeasured(self):
        refuse = Commands()
        for session in [GUI, host_context.session(None, {})]:
            with self.subTest(session["kind"]):
                record = keychain.check(identity=NAME, run=refuse, session=session)
                self.assertEqual(refuse.calls, [])
                self.assertEqual([record[key]["status"] for key in ("keychain", "identity", "signing")],
                                 ["unmeasured"] * 3)
                self.assertEqual(record["outcome"], "unmeasured")
                self.assertIn("dialog", record["keychain"]["said"])

    def test_in_an_ssh_session_unlocked_present_and_signed_passes_with_nothing_that_reads_input(self):
        run = self.commands()
        record = keychain.check(identity=NAME, run=run, session=SSH)
        self.assertEqual((record["keychain"]["status"], record["identity"]["status"], record["signing"]["status"],
                          record["outcome"]), ("unlocked", "present", "passed", "passed"))
        self.assertEqual(record["identity"], {"status": "present", "valid": 1, "matching": 1})
        commands = [command for command, _ in run.calls]
        self.assertEqual(commands[:2], [["security", "show-keychain-info", str(self.login)],
                                        ["security", "find-identity", "-v", "-p", "codesigning", str(self.login)]])
        sign = commands[2]
        self.assertEqual(sign[:7], ["codesign", "--force", "--sign", NAME, "--keychain", str(self.login),
                                    "--timestamp=none"])
        self.assertTrue(sign[7].endswith("/probe"))
        self.assertFalse(Path(sign[7]).exists(), "the signed copy is removed")
        self.assertIn("SSH login", record["session"]["note"])
        # Counts only: no identity's hash or name is kept.
        self.assertNotIn(SHA, json.dumps(record))
        self.assertNotIn(NAME, json.dumps(record))
        # A listing of every matching identity before the valid ones counts each valid one once.
        both = completed([], 0, f"Policy: Code Signing\n  Matching identities\n{LISTED.replace('valid ', '')}\n"
                                f"  Valid identities only\n{LISTED}")
        self.assertEqual(keychain.identity_presence(both, NAME), {"status": "present", "valid": 1, "matching": 1})
        # The identity can be named by its SHA-1 as codesign takes it.
        self.assertEqual(keychain.check(identity=SHA.lower(), run=self.commands(), session=SSH)["outcome"], "passed")
        # A session without graphic access that is not an SSH login runs the same checks.
        background = host_context.session(host_context.AUTHENTICATED, {})
        self.assertEqual(keychain.check(identity=NAME, run=self.commands(), session=background)["outcome"], "passed")

    def test_a_locked_keychain_and_a_refused_signing_fail_each_on_its_own(self):
        refused = "security: SecKeychainCopySettings: User interaction is not allowed.\n"
        record = keychain.check(identity=NAME, run=self.commands(keychain_info=(36, "", refused),
                                sign=(1, "", "probe: errSecInternalComponent\n")), session=SSH)
        self.assertEqual((record["keychain"]["status"], record["identity"]["status"], record["signing"]["status"],
                          record["outcome"]), ("locked", "present", "failed", "failed"))
        self.assertIn("errSecInternalComponent", record["signing"]["said"])
        # A listed identity and an unlocked keychain do not make a signing that failed pass.
        record = keychain.check(identity=NAME, run=self.commands(sign=(1, "", "errSecInternalComponent\n")), session=SSH)
        self.assertEqual((record["keychain"]["status"], record["outcome"]), ("unlocked", "failed"))

    def test_a_missing_keychain_or_identity_fails_and_what_was_not_tried_is_unmeasured(self):
        self.login.unlink()
        refuse = Commands()
        record = keychain.check(identity=NAME, run=refuse, session=SSH)
        self.assertEqual(refuse.calls, [])
        self.assertEqual((record["keychain"]["status"], record["identity"]["status"], record["signing"]["status"],
                          record["outcome"]), ("missing", "unmeasured", "unmeasured", "failed"))
        self.login.write_bytes(b"kych")
        record = keychain.check(identity="Apple Distribution", run=self.commands(), session=SSH)
        self.assertEqual((record["identity"]["status"], record["signing"]["status"], record["outcome"]),
                         ("absent", "unmeasured", "failed"))
        none = self.commands(identity=(0, "     0 valid identities found\n"))
        self.assertEqual(keychain.check(run=none, session=SSH)["identity"], {"status": "absent", "valid": 0})
        gone = "security: SecKeychainCopySettings: The specified keychain could not be found.\n"
        self.assertEqual(keychain.keychain_status(completed([], 50, "", gone))["status"], "missing")

    def test_a_diagnostic_that_gives_no_answer_is_unmeasured_never_passed(self):
        timed_out = subprocess.TimeoutExpired(["security"], 30)
        run = self.commands(keychain_info=timed_out, identity=OSError("no security"), sign=timed_out)
        record = keychain.check(identity=NAME, run=run, session=SSH)
        self.assertEqual((record["keychain"]["status"], record["identity"]["status"], record["signing"]["status"],
                          record["outcome"]), ("unmeasured", "unmeasured", "unmeasured", "unmeasured"))
        self.assertIn("did not finish in 30s", record["keychain"]["said"])
        # Output it cannot read, or an exit it cannot place, says nothing either.
        odd = self.commands(keychain_info=(1, "", "something else\n"), identity=(0, "no count here\n"))
        record = keychain.check(identity=NAME, run=odd, session=SSH)
        self.assertEqual((record["keychain"]["status"], record["identity"]["status"]), ("unmeasured", "unmeasured"))
        self.assertEqual(record["signing"]["status"], "passed")
        # Without an identity named, key use is not measured.
        record = keychain.check(run=self.commands(), session=SSH)
        self.assertEqual((record["identity"]["status"], record["signing"]["status"], record["outcome"]),
                         ("present", "unmeasured", "unmeasured"))
        with patch.object(keychain, "PROBE", str(self.home / "missing")):
            self.assertEqual(keychain.check(identity=NAME, run=self.commands(), session=SSH)["signing"]["status"],
                             "unmeasured")

    def test_ssh_and_gui_sessions_of_one_keychain_are_recorded_apart(self):
        ssh = keychain.check(identity=NAME, run=self.commands(), session=SSH)
        gui = keychain.check(identity=NAME, run=self.commands(), session=GUI)
        self.assertEqual((ssh["session"]["kind"], ssh["outcome"]), ("ssh", "passed"))
        self.assertEqual((gui["session"]["kind"], gui["outcome"]), ("gui", "unmeasured"))
        self.assertNotEqual(ssh["session"]["note"], gui["session"]["note"])


class CommandLineTests(unittest.TestCase):
    def test_eng_keychain_needs_no_project_and_exits_by_the_check_s_outcome(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(empty)]))
        for outcome, status in [("passed", 0), ("failed", 1), ("unmeasured", evidence.UNMEASURED)]:
            with self.subTest(outcome), patch.object(keychain, "check", return_value={"outcome": outcome}) as made, \
                    patch("sys.stdout", new_callable=StringIO) as printed:
                self.assertEqual(cli.main(["--project", str(empty), "keychain", "check", "--identity", NAME,
                                           "--keychain", "build.keychain", "--timeout", "5"]), status)
                self.assertEqual(made.call_args.args, ("build.keychain", NAME, 5.0))
                self.assertEqual(json.loads(printed.getvalue())["outcome"], outcome)
        with patch.object(keychain, "recover", return_value=2) as recovering:
            self.assertEqual(cli.main(["--project", str(empty), "keychain", "recover", "--target", SECOND]), 2)
            self.assertEqual(recovering.call_args.args[:3], (keychain.DEFAULT, SECOND, None))

    def test_a_check_in_this_session_runs_nothing_that_could_prompt_when_it_can_show_a_dialog(self):
        # This Mac's own session, read for real: a session with graphic access runs no security command.
        session = host_context.session()
        if session["graphicAccess"] is False:
            self.skipTest("this session cannot show a dialog, so the check would run security")
        refuse = Commands()
        record = keychain.check(identity=NAME, run=refuse)
        self.assertEqual((refuse.calls, record["outcome"]), ([], "unmeasured"))


if __name__ == "__main__":
    unittest.main()
