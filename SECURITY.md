# Security

Queuekeeper runs commands on your Macs, reaches other Macs over SSH, and can keep a Mac's login
password to unlock its keychain for a run. Please report a weakness in any of that privately,
through GitHub's [private vulnerability reporting](https://github.com/vocaro/queuekeeper/security/advisories/new)
for this repository, not in a public issue.

## Reporting a vulnerability

Say what an attacker could do, on which version or commit, and how to reproduce it.

You should hear back within a week. Fixes land on `main`; there are no maintained release branches,
so the fix is in the next version.

## Scope

In scope: the `qk` command, the merge queue, admission, the build farm's SSH use, the dashboard and
the plugins in this repository.

Out of scope: the commands a project's own lanes run, and the security of the Macs, SSH keys and Git
remotes you give Queuekeeper, which it uses as you configured them.
