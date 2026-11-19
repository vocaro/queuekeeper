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

From the project's directory:

```sh
claude mcp add --scope project queuekeeper -- python3 ~/Development/queuekeeper/plugins/mcp/src/queuekeeper_mcp/server.py
```

This writes `.mcp.json` in the project, with the path the shell expanded, so every session there
gets the tools, as `mcp__queuekeeper__land` and so on. Use `--scope user` to have it in every
project instead, or `--scope local` to keep the path out of the repository. Tools run in the
server's working directory unless they name a `directory`; an agent in a worktree should pass its
worktree to `land`. Claude Code waits a long time for a tool by default; `MCP_TOOL_TIMEOUT`, in
milliseconds, changes that.

## Register it with Codex

Add it to `~/.codex/config.toml`:

```toml
[mcp_servers.queuekeeper]
command = "python3"
args = ["/Users/YOU/Development/queuekeeper/plugins/mcp/src/queuekeeper_mcp/server.py"]
tool_timeout_sec = 900
```

Codex gives a tool 60 seconds by default, so raise `tool_timeout_sec` above the `limit` your
agents pass to `queue_wait`. Add `"--project", "/path/to/project"` to `args` to pin the
directory tools run in when they name none.

## Install it as a package

`pip install plugins/mcp` installs a `queuekeeper-mcp` command that runs the same server.
