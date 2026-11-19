"""The device pass under its earlier name: a project script that calls `device_pass.run` from Python finds it here,
with the Apple platform switched on, though it is the mobile extension's now (queuekeeper_mobile.device_pass)."""
from queuekeeper_mobile.device_pass import *  # noqa: F401,F403
from queuekeeper_mobile.device_pass import main, run  # noqa: F401
from . import apple_platform  # noqa: F401 -- switches the Apple platform on

if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
