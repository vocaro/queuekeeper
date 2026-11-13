# The build farm

The build farm is the Mac you work on plus any other Mac you can reach over SSH. Queuekeeper decides
which one runs each lane.

## Which Mac runs a lane

A lane is offered first to another Mac and runs on the starting Mac when the other has no room. When
neither has room, it runs on whichever admits it first. The tool decides, by reading each Mac's free
slots, CPU busy share and memory pressure. The agent does not.

- **Gate hosts.** The queue's gates can use a list of Macs, tried in order. A rented Mac with SSH
  can be one of them.
- **Exclusive work.** Exclusive work sent to another Mac is offered until it can start there at once,
  and takes a place in that Mac's queue only after 15 minutes. A queued exclusive request holds every
  later request back, so queuing it from the start left a Mac idle while it waited. A heavy lane's own
  command is never admitted as exclusive work.
- **Unreachable hosts.** A Mac that cannot be reached is skipped and the lane runs locally. A Mac
  that is reachable but set up wrongly, with a different Xcode build for example, runs nothing and
  says so, instead of producing results that cannot be compared.

## How the code gets there

The exact commit, or a snapshot commit of the working tree, is pushed over SSH straight to the other
Mac. Nothing goes through the Git host. The other Mac keeps four checkouts per project and reuses
the one closest to the new commit, so builds stay warm. A one-line change sent 282 bytes in one
measurement.

Environment variables do not travel to the other Mac, so lane settings must be arguments.

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
Queuekeeper detects this instead of reporting the other Mac as down, and includes a small signed
launcher that avoids it.

See also [Lanes and admission](lanes_and_admission.md) and the
[Apple plugin](../src/python/queuekeeper/apple/README.md), which keeps Xcode and simulator runtimes
alike across Macs.
