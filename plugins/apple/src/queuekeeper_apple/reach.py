"""Forwards to queuekeeper_mobile.reach, where this module moved on 2026-11-18 when the Android plugin came and the
mobile plugin took what it shares with this one, so that a project still importing queuekeeper_apple.reach keeps
working until it changes (vocaro/queuekeeper#103)."""
from queuekeeper import forwarding

forwarding.forward(__name__, "queuekeeper_mobile.reach", moved="2026-11-18", until="2027-02-18")
