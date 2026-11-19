"""An MCP server over stdio for Queuekeeper's JSON commands: land, queue wait, result, status and logs.

See README.md here for registering it with Claude Code and Codex, and server.py for the protocol."""


def register():
    """Add nothing: the server is a program of its own, which runs the project's launcher, and no plugin. This
    lets the checkout load it like every other component it is composed of."""
