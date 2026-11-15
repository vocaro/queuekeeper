"""Apple protections layered on its declared desktop dependency."""
from test_support import desktop_host
import host_isolation
from host_isolation import os
import queuekeeper_apple as apple
from queuekeeper_apple import app_registrations, device_notifications, device_pass, devices
apple.register()

# Nor does any reach a paired device's Focus (#144): a reservation a test makes that would run the owner's
# notification shortcuts finds devicectl unable to open Shortcuts. A test of them replaces this again.
device_notifications.run = lambda command: (None, "tests never reach a device")
# Nor does any run a test makes, in this process or a child, relay a device's log: its watch starts no relay and
# says so. A test of the watch sets its own relay.
os.environ[device_notifications.WATCH] = "off"


for name in ("apple", "app_registrations", "device_notifications", "device_pass", "devices"):
    setattr(host_isolation, name, globals()[name])
