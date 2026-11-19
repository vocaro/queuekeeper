"""Forwards to queuekeeper.plugins, where this module moved on 2026-11-19 when Queuekeeper's extensions took the name
plugins, so that a project's own tooling still importing queuekeeper.extensions keeps working until it changes
(vocaro/queuekeeper#48, #103)."""
from queuekeeper import forwarding

forwarding.forward(__name__, "queuekeeper.plugins", moved="2026-11-19", until="2027-02-19")
