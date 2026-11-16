# The build farm

The build farm is the Mac you work on plus any other Mac you can reach over SSH. Queuekeeper decides
which one runs each lane.

## Adding and removing Macs

The farm's Macs are the ones you add, on the Mac you work on. Nothing else names one: Queuekeeper
looks for no SSH alias and reads no variable.

```sh
qk farm add DESTINATION [--name NAME]   # reach the Mac over SSH and add it, last in the order
qk farm list                            # the Macs, in the order work is offered to them
qk farm rename NAME NEW
qk farm pause NAME [--reason WHY]       # offer it no work until `qk farm resume NAME`
qk farm remove NAME                     # take it out for good
qk farm foreground NAME [--off]         # let lanes that take a Mac's screen use it, or not
```

- **Names.** A Mac is called by its own local host name unless you give another, and you can rename
  it later. Records keep its hardware identifier, so a renamed Mac's history stays its own.
- **Pausing.** A paused Mac stays listed and is offered nothing. What already runs there finishes.
- **Removing.** A removed Mac is no longer offered work or reported anywhere. Removal is refused
  while work this Mac sent is still running there. Its checkouts stay on it until you delete them.
- **One Mac, once.** Adding is refused for the Mac you are on and for a Mac the farm already has
  under another name or address.

## Which Mac runs a lane

A lane is offered to the farm's other Macs in their order, starts on the first with room, and runs on the
starting Mac when none of them has room. When no Mac has room, it runs on whichever admits it first. The tool decides, by reading each Mac's free
slots, CPU busy share and memory pressure. The agent does not.

- **Any number of Macs.** Lanes, admitted commands and the queue's gates use every Mac of the farm, tried
  in order. A rented Mac with SSH can be one of them.
- **Exclusive work.** Exclusive work sent to another Mac is offered until it can start there at once,
  and takes a place in that Mac's queue only after 15 minutes. A queued exclusive request holds every
  later request back, so queuing it from the start left a Mac idle while it waited. A heavy lane's own
  command is never admitted as exclusive work.
- **Unreachable hosts.** A Mac that cannot be reached is skipped and the lane runs locally. A Mac
  that is reachable but set up wrongly, with a different Xcode build for example, runs nothing and
  says so, instead of producing results that cannot be compared.

## How the code gets there

The exact commit, or a snapshot commit of the working tree, is pushed over SSH straight to another
Mac. Nothing goes through the Git host. Another Mac keeps four checkouts per project and reuses
the one closest to the new commit, so builds stay warm. A one-line change sent 282 bytes in one
measurement.

Environment variables do not travel to another Mac, so lane settings must be arguments.

## Asking whether a command runs on another Mac

A command that Queuekeeper sent to another Mac, a lane's or `qk admit`'s alike, finds
`QUEUEKEEPER_DISPATCHED_FROM` in its environment, holding the name of the Mac that sent it. A
command that runs on the Mac where it started does not. A script or test that must know which of
the two it is asks only whether the variable is set:

```sh
if [ -n "${QUEUEKEEPER_DISPATCHED_FROM:-}" ]; then
    echo "sent here by another Mac"
fi
```

Do not compare a host name or a user ID instead. macOS renames a Mac whose name another machine on
the network claims, and a check written for the old name then refuses every Mac without saying
why. Do not compare the variable's value either: it is the sending Mac's name, which changes the
same way.

The variable describes the run, not the Mac. A command started by hand in a shell on another
Mac was sent by nobody and does not have it.

## A dropped connection

A run is journaled on the Mac that executes it. If the starting Mac sleeps or the connection drops,
the run continues, and the starting Mac reattaches and collects the result.

## Screen lock

Tests that drive a user interface fail in confusing ways behind a locked screen. A lane marked
`"unlockedScreen": true` runs only on a Mac whose screen reads unlocked and waits while none does. A
run that ends unmeasured with the screen locked is not counted against the commits. Queuekeeper
cannot unlock a screen.

## macOS Local Network privacy

macOS can silently deny local-network access to a background process whose parent has exited.
Queuekeeper detects this instead of reporting another Mac as down, and includes a small signed
launcher that avoids it.

See also [Lanes and admission](lanes_and_admission.md) and the
[Apple plugin](../plugins/apple/src/queuekeeper_apple/README.md), which keeps Xcode and simulator runtimes
alike across Macs.
