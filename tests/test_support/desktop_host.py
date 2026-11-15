"""Desktop protections, without Apple or Unity."""
import host_isolation
from host_isolation import ROOT, os
from queuekeeper.checkout import activate
activate()
import queuekeeper_mac_desktop as mac_desktop
from queuekeeper_mac_desktop import crash_alerts, keychain, notification_center, opened_apps
mac_desktop.register()

# Nor does any reach the host's crash-report setting or its screen's alerts (#118): a foreground run a test
# makes reads DialogType unset and no alert, and changes nothing. A test of either replaces these again.
os.environ["QUEUEKEEPER_CRASH_DIALOG_RECORD"] = str(ROOT / "crash-dialog.json")
crash_alerts.run = lambda command, environment=None: "" if command[0] == "osascript" else None
crash_alerts.change = lambda value: True
# Nor does any reach the screen's Notification Center windows (#204): a foreground run a test makes on a second Mac
# finds none, and closes nothing. A test of them replaces this again.
notification_center.run = lambda command, environment=None: "[]" if command[:3] == ["osascript", "-l", "JavaScript"] \
    else None
# Nor does any quit an app on this Mac (vocaro/queuekeeper#104): a foreground run a test makes, in this process or a
# child, reads no running app, so it finds none it opened, and asks none to quit. A test of them replaces both again.
os.environ[opened_apps.SWITCH] = "off"
opened_apps.run = lambda command, environment=None: "[]" if command[-1] == opened_apps.LIST_SCRIPT else None
opened_apps.alive = lambda pid: False

for name in ("mac_desktop", "crash_alerts", "keychain", "notification_center", "opened_apps"):
    setattr(host_isolation, name, globals()[name])
