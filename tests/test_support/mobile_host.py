"""Mobile protections layered on its declared desktop dependency, with a platform of the tests' own."""
from test_support import desktop_host  # noqa: F401
import queuekeeper_mobile as mobile
mobile.register()
