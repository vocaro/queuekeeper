# Setting up another Mac

This guide is the owner's checklist for preparing another Mac to take work, and for letting its runs
sign. Read [Add another Mac](../user_guide.md#add-another-mac) and
[The build farm](../user_guide.md#the-build-farm) in the user guide first; this guide adds the
details they leave out: SSH naming, tools, simulator runtimes, credentials and keychains.

## Check readiness first

```sh
eng setup --check
```

Run it from the workstation. It reports this Mac's readiness and checks the configured Mac over
trusted SSH, including read access to the private toolkit, without changing anything. Use
`--local` for one Mac, or `--signing` to include the kept keychain setup. The keychain check reads
file metadata only; use a signing workflow to prove the key works. The report names each incomplete
check and the step below that fixes it.

## The setup steps

The owner prepares another Mac once.

1. **Remote Login and SSH.** Turn on Remote Login (System Settings, General, Sharing) and add this
   Mac's public key to that Mac's `~/.ssh/authorized_keys`.
2. **An SSH alias.** Name it in this Mac's `~/.ssh/config` so that `ssh ALIAS true` succeeds without
   a prompt, then add it to the farm by that alias ([the farm's Macs](another-mac.md#the-farms-macs)):

   ```
   Host ALIAS
       HostName NAME.local
       AddressFamily inet
   ```

   ```sh
   python3 scripts/qk farm add ALIAS
   ```

   `AddressFamily inet` keeps ssh on IPv4: the `.local` name resolved first to an IPv6 address that
   never answered, so each connection waited out the 10 s `ConnectTimeout` (2026-10-27). `qk doctor`
   reports a first address that keeps every connection waiting as an error.
3. **Xcode.** Install the Xcode this Mac uses, at the same path, and follow the first-launch
   instructions from the setup report.
4. **Homebrew and tools.** Install Homebrew there (https://brew.sh; its installer asks for that
   Mac's password), then install the tools from this Mac ([tools](#tools)).
5. **Simulator runtimes.** Give the Macs the same runtimes ([simulator runtimes](#simulator-runtimes)).
6. **Power.** Keep it on power and awake. A sleeping Mac leaves its run unmeasured. Its `.local`
   name is multicast DNS and resolves only while it is awake on this Mac's network;
   `dscacheutil -q host -a name NAME.local` shows whether it does.
7. **A GitHub token.** Write a read-only token there for private sources
   ([private sources](#private-sources)).
8. **Keychain, for work that signs.** From this Mac, in your own Terminal, run
   `python3 scripts/qk keychain keep-unlocked --target MAC` once and type that Mac's login password
   at its prompt ([Keeping another Mac's keychain unlocked](#keeping-another-macs-keychain-unlocked)).

Then check the Mac with a short lane first, such as this toolkit's `python3 scripts/qk remote run gate`.

### When macOS renames the Mac

macOS renames a Mac whose `.local` name another machine claims, adding or raising a `-N` suffix.
The old `HostName` then does not resolve, or reaches a Mac with another host key.

- **Queuekeeper finds it again.** Every ssh connection it makes, when the name does not resolve or
  answers with another key, searches the Macs that Bonjour lists (`dns-sd -B _ssh._tcp`) for the one
  presenting the host key `known_hosts` holds for the old name (or for `HostKeyAlias`). When exactly
  one does, the run uses it and says `queuekeeper: build-host was renamed: …`; `qk doctor` reports
  the rename as an error until `~/.ssh/config` names it (vocaro-engineering#129). None, or more than
  one, means the Mac cannot be reached, and the failure says what Bonjour listed. The search costs
  about 2 s.
- **A name that answers is not searched.** When an address answered on the ssh port but
  `ssh ALIAS true` then failed or timed out, no rename search runs, since Bonjour's list says
  nothing about why (vocaro/queuekeeper#25).
- **To keep the new name**, put it in `HostName` and add `HostKeyAlias` with the old `HostName`, so
  a later rename needs only a new `HostName`. Giving the Mac a `LocalHostName` nobody else is likely
  to claim (`sudo scutil --set LocalHostName NAME` there) makes renames rarer. Both settings are the
  owner's; Queuekeeper changes neither and says what to set.
- **Don't depend on the name.** A project's script asks whether `QUEUEKEEPER_DISPATCHED_FROM` is set
  ([how a run reaches another Mac](another-mac.md#how-a-run-reaches-another-mac)).

### Tools

```sh
python3 scripts/qk remote setup [FORMULA ...] [--import MODULE ...]
```

- It puts Homebrew on the login shell's PATH in `~/.zprofile`, since `qk remote` runs in that Mac's
  login shell, and installs Homebrew's Python, `uv`, `shellcheck`, `xcodegen`, `zstd` and each
  formula named. A project's runbook names its own formulae.
- A new login shell then checks every tool, each `--import` module and this Mac's Xcode, one line
  each; the command fails while anything is missing. Without Homebrew it stops with nothing
  changed. `--check` only checks.
- For a project whose extensions load from a folder beside it, it checks that the other Mac has its
  own checkout of each and names the `git clone` to run, but does not clone
  ([Extension folders beside a project](another-mac.md#extension-folders-beside-a-project)).
- Without Homebrew, the system's Python 3.9 runs the toolkit, and lanes that need a newer Python fail.

### Simulator runtimes

Run this at setup and again whenever either Mac gains a runtime; `qk doctor` runs it itself when
the runtimes differ:

```sh
python3 scripts/qk remote setup --runtimes [--check]
```

It makes both Macs hold the same runtimes of this Mac's Xcode, which must be at the same path on both.

- **Unused, on one Mac only: deleted** there (`xcrun simctl runtime delete`), checked again just
  before. A superseded build counts as unused unless the toolkit pins it or that Mac's device record
  names it, as [`qk disk` decides](disk-cleanup.md#reclaiming-disk-space); this Mac sends its pins
  with each request (vocaro-engineering#190).
- **Used, or unlistable: copied** to the other Mac as its own disk image, over SSH, never GitHub, so
  a beta build Apple no longer serves comes across exactly. Only a runtime whose image cannot be
  read is downloaded instead (`xcodebuild -downloadPlatform PLATFORM -buildVersion BUILD`).
- **No delete while any simulator runs** on that Mac, on any runtime: deleting a runtime can crash
  the launch host they share ([#154](https://github.com/vocaro/queuekeeper/issues/154)). The run
  names the simulators and exits 1; run it again once none runs.
- **40 GiB floor.** It refuses a copy or download that would leave less than 40 GiB free where it
  lands, room for simctl's own copy and other sessions' builds.
- **Staging.** Images go to `~/Development/queuekeeper-farm/runtimes` on another Mac or
  `~/Library/Application Support/queuekeeper/runtimes` here, outside `/private/tmp`, and are added
  with `xcrun simctl runtime add --move`. A failed or stopped copy adds nothing, and the next run
  removes what it left.
- **Result.** It exits 0 only when the Macs' runtimes match, else 1 with what differs. `--check`
  says what it would do and changes nothing. One copy runs at a time. Five runtimes move about
  40 GB, so run it in the background; its last line is the outcome.

### Private sources

Project commits reach another Mac over SSH with no GitHub credential. Two things still need one: a
heavy lane's policy refresh, which fetches the private toolkit over HTTPS, and SwiftPM's clones of
private dependencies. git's default helper, osxkeychain, cannot read the keychain over SSH (it fails
with `-25308`; [Keychains on the right Mac](#keychains-on-the-right-mac)).

Create a fine-grained GitHub token with read-only Contents permission on
`vocaro/vocaro-engineering` and the private dependencies (or all of the owner's repositories), and
write it on the other Mac as one line `https://USER:TOKEN@github.com`, readable only by its owner,
without leaving it in zsh's history:

```
read -rs "token?Token: "; echo
(umask 077; printf 'https://USER:%s@github.com\n' "$token" > ~/Development/queuekeeper-farm/git-credentials)
unset token
```

`qk remote` gives each run's git that file through the store helper alone, by `GIT_CONFIG_COUNT`
variables that reach only the run, so the owner's git configuration there is unchanged. The token
expires: renew it before then.

Private GitHub release assets also need the authenticated API. Declare their packages in
`githubArtifacts`; admitted builds use the shared
[artifact preparation](https://github.com/vocaro/vocaro-engineering/blob/main/doc/guides/public-access.md#private-binary-artifacts-and-build-credentials),
which reads the same helper. Never put a token in a package manifest, asset URL or committed
configuration.

## Keychains on the right Mac

A keychain unlock reaches one Mac and only the sessions it was made in. Unlocking the workstation
does nothing for another Mac, and an unlocked desktop says nothing about a process SSH started.
An SSH alias exists only on the Mac whose `~/.ssh/config` defines it
([vocaro-engineering#65](https://github.com/vocaro/vocaro-engineering/issues/65)).

- **Before asking the owner to unlock a keychain**, establish which Mac the work needs and which Mac
  and session the owner's shell is in, and give only commands that check both.
- **Never ask for the password**, and never put it in chat, a command's arguments, an environment
  variable, a log or evidence. `security unlock-keychain` asks for it in the owner's own Terminal.

```sh
python3 scripts/qk keychain recover [--target MAC] [--keychain PATH] [--identity NAME]
python3 scripts/qk remote admit -- python3 scripts/qk keychain check [--keychain PATH] --identity NAME
```

### `qk keychain recover`

The owner's command. It unlocks nothing and reads no password. It:

- names this Mac, by LocalHostName, and its kind of session;
- finds the target: by default the farm's first Mac that takes work now, or `--target MAC` by
  LocalHostName. Run on another Mac, it targets that Mac itself and prints the local command;
- refuses, saying what to do instead, when the target is neither this Mac nor a farm Mac, or cannot
  be reached;
- checks the keychain file exists on the target under its exact name. `--keychain` takes a path or
  a file name in `~/Library/Keychains` and never completes one; a near miss lists the files there;
- prints the unlock commands. Each runs only on the Mac it names and fails elsewhere, and the
  `ssh -t ALIAS …` form is printed only where the alias is defined;
- prints `qk keychain check` last.

### `qk keychain check`

Run it where the automation that uses the key runs: on another Mac, through `qk remote admit`, as
every remote run is. It prints a JSON record with four results:

- `session`: `ssh`, `gui` or `background`, from the process's audit session flags
  ([probing a session](https://github.com/vocaro/apple-engineering/blob/main/guides/background-mac-testing.md#probing-a-session)).
- `keychain`: `unlocked`, `locked` (errSecInteractionNotAllowed) or `missing`.
- `identity`: whether `security find-identity -v -p codesigning` lists the `--identity` (SHA-1 or
  part of a name), else any valid code-signing identity. Counts only, never names or hashes.
- `signing`: whether `codesign` can sign a temporary copy of `/usr/bin/true` with it.

The outcome is `passed` (exit 0) only when signing passed; `failed` (exit 1) when signing failed or
the keychain or identity is missing; otherwise `unmeasured` (exit 3), with each cause. A timeout
(30 s each, `--timeout`), an unreadable answer, or no `--identity` leaves it unmeasured. It runs
`security` and `codesign` only in a session without graphic access, where they cannot raise a
dialog; elsewhere it records them unmeasured.

### What a result means

- **A result holds only for the kind of session it was measured in.** Signing in the owner's GUI
  session does not qualify signing over SSH, nor the reverse.
- **An unlock made beforehand does not reach a `qk remote` run**, whether made in Keychain Access or
  in an earlier `ssh -t` session (2026-10-29,
  [vocaro-engineering#65](https://github.com/vocaro/vocaro-engineering/issues/65)). So each run
  unlocks the keychain itself ([below](#keeping-another-macs-keychain-unlocked)). After any change to
  how a key is reached, run the check, and retry signing work only once it passes where the work runs.
- **Access control stays the owner's.** Partition lists, trust settings, certificates, TCC, host
  configuration and where credentials are kept are the owner's decisions; neither command changes
  them, and a failed check is reported for that decision.
- Not measured: another Mac's own `recover` run from a shell on that Mac.

## Keeping another Mac's keychain unlocked

The owner keeps another Mac's login keychain unlocked for its automation, since it holds no
sensitive data and stays in a secure place (the owner's decision, 2026-10-30, replacing that of
2026-10-29 to sign only in the GUI session there). Every `qk remote` connection is its own SSH
login, so each run unlocks the keychain itself with the login password the owner gives once:

```sh
python3 scripts/qk keychain keep-unlocked [--target MAC] [--identity NAME]
```

The owner runs it in their own Terminal, on the sending Mac or in a shell on the other Mac, once
for each farm Mac whose runs sign. It picks the target as `recover` does (`--target` by its name in
`qk farm list` or its LocalHostName). Then, on that Mac, it:

- refuses, having asked for nothing, on the wrong Mac, without a terminal, or before `qk remote` has
  made `~/Development/queuekeeper-farm`;
- asks for the login password at that Mac's own prompt and tries it on
  `~/Library/Keychains/login.keychain-db`, three times at most; a password that fails is not kept;
- keeps the one that works in `~/Development/queuekeeper-farm/keychain-password`, readable only by
  the owner's account;
- sets that keychain to lock neither after a time nor on sleep, and prints its settings;
- exits 0 when done, 1 when no password unlocked it or the settings were not set, 2 when it refused.

Each `qk remote run` and `qk remote admit` then unlocks the login keychain from that file before it
claims a checkout, in the run's own session, and says so in one line. The password goes to Security
only. Without the file a run unlocks nothing. When the password no longer works, for example after
a password change, the run is refused before it starts, and the fix is to run `keep-unlocked` again;
the work does not move to this Mac.

It reaches `qk remote`'s runs only: a plain `ssh ALIAS` session still finds the keychain locked
(measured 2026-10-30). To undo it, delete the file on the other Mac and set the lock settings you
want with `security set-keychain-settings`.

## Earlier names

Until 2026-10-28 another Mac was called the build host, though both Macs build. Since
[vocaro-engineering#58](https://github.com/vocaro/vocaro-engineering/issues/58) Queuekeeper refuses
each old configuration name, naming its replacement, rather than silently moving the work:

- `"buildHost"` on a lane or group: configuration validation refuses it. Delete it; every heavy or
  exclusive lane goes to another Mac unless it says `here`.
- `qk run --build-host-only`: refused; use `--offload-only`.
- `devices recover --build-host-unchecked REASON`: refused; use `--offload-unchecked REASON`.

Words nothing reads are not refused. Receipts and records written before keep `build host` and
`buildHost`, and such a receipt still counts: `qk remote passed` and `eng release` read receipts by
commit, whatever their `ranOn`.

Since the farm got its own list ([the farm's Macs](another-mac.md#the-farms-macs)), Queuekeeper no
longer reads what named the one other Mac, and does not refuse it: `QUEUEKEEPER_OFFLOAD_HOST`,
`VOCARO_BUILD_HOST`, `QUEUEKEEPER_GATE_HOSTS`, the `gate-hosts` file, and the aliases `build-host`
and `second-mac`, now aliases like any other. Such a Mac takes work again once `qk farm add` adds
it; until then every lane runs on this Mac. Adding it moves its `~/Development/build-host` to
`~/Development/queuekeeper-farm`.

Records made before a rename, and the guides' dated observations, keep their words.
