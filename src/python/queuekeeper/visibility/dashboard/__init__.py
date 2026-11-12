"""A read-only web page of the queue, served on this Mac alone: `queuekeeper serve`.

Home says in a glance what the queue is doing and whether it needs its owner; the other tabs hold the detail.
Each tab is a panel, a module of its own in `panels` with a script of its own beside it, found by its name: adding
one changes no other file, and one that fails shows its fault in its own tab (panels). The server (server) only
reads: the queue's snapshot and journal, its logs, and the Macs' admissions. It changes nothing, takes no request
but GET, and answers only a browser that asked for this Mac by name (doc/visibility.md).
"""
from ... import extensions

HELP = "serve a read-only web page of the queue on this Mac"


def register():
    from . import registry, server
    extensions.COMMANDS["serve"] = (HELP, server.main)
    extensions.AFTER_EVENT.append(registry.note)
