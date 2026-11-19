"""What the mobile plugins add to `qk doctor`: the devices the owner declared, as each Mac reaches them now."""
from queuekeeper import plugins


def declared_devices(root, config):
    """(errors, warnings) of the devices the owner declared, as each Mac reads them now (reach.findings): a device
    declared always reachable that no Mac has reached for the settle time is an error."""
    from queuekeeper import offload_host
    from . import reach
    try: return reach.findings(offload_host.record_path())
    except (OSError, ValueError, RuntimeError) as error:
        return [], [f"the declared devices could not be read: {error}"]


def overdue_silences(root, config, record=None):
    """A warning for each held physical device past its expected release whose silencing of the owner's
    notifications is not yet put back (devices.overdue_silences; vocaro/queuekeeper#174). It changes nothing."""
    from . import devices
    try: value = (record or devices.Record()).load()
    except (OSError, ValueError, RuntimeError) as error:
        return [f"the device record could not be read to look for an overdue silencing: {error}"]
    return devices.overdue_silences(value["devices"])


def register():
    if overdue_silences not in plugins.DOCTOR: plugins.DOCTOR.append(overdue_silences)
    if declared_devices not in plugins.DOCTOR_CHECKS: plugins.DOCTOR_CHECKS.append(declared_devices)
