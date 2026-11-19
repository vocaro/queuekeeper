# devicectl listings for the reach reader

What `xcrun devicectl list devices --quiet --json-output FILE` (devicectl 651.32, Xcode 27.1) printed on the
owner's two Macs on 2026-11-17, for `queuekeeper_apple.device_listing` and `queuekeeper_mobile.reach` (vocaro/queuekeeper#211). They keep the shape of
each listing and none of its identifiers: every UDID, CoreDevice identifier and name is invented, serial numbers,
ECIDs, host names and tunnel addresses are left out, and of the 67 or more simulators each Mac listed, two are kept.

- `cable.json`: the owner's workstation, with the iPad and the iPhone attached by cable. Both read `wired`;
  the iPad's `tunnelState` is `connected` and the iPhone's `disconnected`, though both were attached, which is
  why the reader takes reach from `transportType` alone.
- `network.json`: the other Mac, a minute later, which reads both as `localNetwork`, `tunnelState`
  `disconnected`.

A simulator reads `transportType` `sameMachine` and `reality` `simulated`, and the reader leaves it out. No
listing of a paired device that no way reaches was recorded; the tests take a device missing from a listing as
not reached, and one listed with no transport as listed by no way a declaration may name.

Replace or extend them only with listings recorded the same way, their identifiers invented again.
