"""Apple protections layered on its declared desktop and mobile dependencies."""
from test_support import desktop_host
import host_isolation
from host_isolation import os
import queuekeeper_apple as apple
import queuekeeper_mobile as mobile
from queuekeeper_apple import app_registrations, device_notifications
from queuekeeper_mobile import device_pass, devices
mobile.register()
apple.register()

# Nor does any reach a paired device's Focus (#144): a reservation a test makes that would run the owner's
# notification shortcuts finds devicectl unable to open Shortcuts. A test of them replaces this again.
device_notifications.run = lambda command: (None, "tests never reach a device")
# Nor does any run a test makes, in this process or a child, relay a device's log: its watch starts no relay and
# says so. A test of the watch sets its own relay.
os.environ[device_notifications.WATCH] = "off"


for name in ("apple", "app_registrations", "device_notifications", "device_pass", "devices"):
    setattr(host_isolation, name, globals()[name])
