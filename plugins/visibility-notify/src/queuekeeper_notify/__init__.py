"""Tells the owner when the queue needs them: a banner, a command or a webhook: `qk notify`.

Each time the queue writes an event that a rule is on for, the process that wrote it starts another, apart from
itself, that reads the journal from where the last one stopped and sends a notice of each such event through every
channel that is on (deliver). The queue waits for none of it. A rule decides which events are told (rules); a
channel, a module of its own in `channels`, is one way of telling. A queue that stalls writes no event, so the
commands that reach the hosts also start, at most once every two minutes, a look for one that is stuck (check). The
feature's settings are the `notify` object of the owner's visibility settings.
"""
from queuekeeper import extensions

HELP = "tell the owner when the queue needs them, and say which rules and channels are on"


def register():
    from . import check, commands, deliver
    extensions.COMMANDS["notify"] = (HELP, commands.main)
    extensions.AFTER_EVENT.append(deliver.spawn)
    extensions.HOST_COMMAND.append(check.spawn)
