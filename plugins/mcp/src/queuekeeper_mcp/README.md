# The MCP server

An agent that can keep a long command running in the background needs nothing here: it runs
`qk land --wait --json` and follows it to its result, as [the user guide's agent
section](../../../../doc/user_guide.md#for-agents) describes. This server is for agents that cannot.
It offers the same JSON commands as Model Context Protocol tools over stdio:

| Tool | Runs | Notes |
|---|---|---|
| `land` | `qk land [REFERENCE] --json` | Submits and returns at once with the entry's `id` |
| `queue_wait` | `qk queue wait ID... --limit=SECONDS --json` | Each progress line becomes an MCP progress notification |
| `queue_result` | `qk queue result ID --json` | Exit status 0 means the lookup succeeded; read `state` and `terminal` |
| `queue_status` | `qk queue status --json` | |
| `queue_logs` | `qk queue logs ID --json [--cursor=CURSOR]` | Pass `nextCursor` back unchanged while `hasMore` is true |

Each tool returns the command's JSON as both text and `structuredContent`, with the command's
`exitStatus` added when the result lacks one. Exit statuses mean what they do on the command line:
for `queue_wait`, 0 is landed, 1 refused or sent back, and 75 held or the wait reached its limit. A
tool reports an error only when the command could not run, printed no JSON, or exited 2 or 70.

`queue_wait` stops after 600 seconds unless given another `limit`, because MCP clients time out
long tool calls. Ending a wait, or cancelling the call, withdraws nothing: call `queue_wait` again to
keep following the entry.

Every tool takes an optional `directory`, the checkout or worktree to run in. It defaults to the
server's working directory, or `--project DIR`. The server runs the nearest `scripts/qk` at or above
that directory, the project's own launcher, or the one `--launcher PATH` names. Give `land` the
worktree that holds the commit, since it lands that worktree's HEAD by default.

The server uses only the standard library and imports nothing from Queuekeeper, so it adds no
runtime dependency. It speaks JSON-RPC 2.0, one message a line, and accepts protocol versions
2024-11-05, 2025-03-26 and 2025-06-18.

## Register it with Claude Code

Tried with Claude Code 2.1.321. From the project's directory:

```sh
claude mcp add queuekeeper -- python3 ~/Development/queuekeeper/plugins/mcp/src/queuekeeper_mcp/server.py
```

This uses the default scope, `local`: the server is private to you and recorded in `~/.claude.json`
under the project. Run from a linked worktree, it is recorded under the main checkout's path, so
it serves that checkout and every worktree of it. `--scope user` has it in every project, and
`--scope project` writes it to `.mcp.json` in the repository, where each person approves it before
it connects. `claude mcp get queuekeeper` should say `Connected`; `claude mcp remove queuekeeper -s
local` removes it.

The tools appear as `mcp__queuekeeper__land`, `mcp__queuekeeper__queue_wait` and so on. Claude Code
starts the server in the session's working directory, so a session started in a worktree runs its
tools there and `land` lands that worktree's HEAD. A session that commits somewhere else should pass
that worktree as `directory`.

Claude Code sends a progress token with every call, but in 2.1.321 its terminal did not show
`queue_wait`'s progress lines, and they do not extend its tool timeout. That timeout is about 28
hours by default; `MCP_TOOL_TIMEOUT`, or a `timeout` field on the server in `.mcp.json`, both in
milliseconds, lower it. When it expires, or the user presses Escape during a call, Claude Code sends
`notifications/cancelled` and the server ends the command. The entry stays queued. Claude Code stops
the server with SIGINT and then SIGTERM, and either one ends the commands it is running.

## Register it with Codex

Tried with Codex 0.151.0. Add it to `~/.codex/config.toml`:

```toml
[mcp_servers.queuekeeper]
command = "python3"
args = ["/Users/YOU/Development/queuekeeper/plugins/mcp/src/queuekeeper_mcp/server.py"]
tool_timeout_sec = 900
default_tools_approval_mode = "approve"
```

Codex starts the server in its working directory, the project or worktree it was started in. Add
`"--project", "/path/to/project"` to `args` to pin the directory that tools use when they name
none.

Without `default_tools_approval_mode = "approve"`, Codex asks before each tool call, and
`codex exec` under `approval_policy = "never"` fails every call with "MCP tool call requires
approval".

In Codex 0.151.0, the [default MCP tool timeout](https://github.com/openai/codex/blob/rust-v0.151.0/codex-rs/codex-mcp/src/rmcp_client.rs#L97-L98)
is 300 seconds. Keep `tool_timeout_sec` above the `limit` your agents pass to `queue_wait`,
which is 600 seconds by default. `queue_wait`'s progress does not extend the timeout.

In `codex exec` 0.151.0, a tool timeout failed the call without sending
`notifications/cancelled`. In the interactive terminal UI of the same version, pressing Escape
interrupted the turn while a synthetic `queue_wait` was running. The server received neither a
cancellation notification nor a signal, and the launcher ran to completion. A wait can therefore
keep running until its own limit or until the server stops. The queue continues processing the
entry independently. Call `queue_wait` with the same ID to follow it again.

## Install it as a package

`pip install plugins/mcp` installs a `queuekeeper-mcp` command that runs the same server.
