"""Android protections layered on its declared desktop and mobile dependencies."""
from test_support import desktop_host  # noqa: F401
import host_isolation
import queuekeeper_android as android
import queuekeeper_mobile as mobile
from queuekeeper_android import android_platform
mobile.register()
android.register()

for name in ("android", "android_platform"):
    setattr(host_isolation, name, globals()[name])
