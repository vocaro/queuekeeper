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


def overdue_silences(root, config, record=None, *, repair=True):
    """What Queuekeeper put right in the device record by itself just now (upkeep: a run proven gone, the owner's
    notifications past the grace), then a warning for each held physical device past its expected release whose
    silencing of the owner's notifications is still not put back (devices.overdue_silences; vocaro/queuekeeper#174)."""
    from . import devices, upkeep
    record = record or devices.Record()
    try:
        done = [f"put right by itself: {line.removeprefix('devices: ')}" for line in upkeep.upkeep(record)] if repair else []
        value = record.load()
    except (OSError, ValueError, RuntimeError) as error:
        return [f"the device record could not be read to look for an overdue silencing: {error}"]
    return done + devices.overdue_silences(value["devices"])


def register():
    if overdue_silences not in plugins.DOCTOR: plugins.DOCTOR.append(overdue_silences)
    plugins.DOCTOR_WITHOUT_REPAIRS[overdue_silences] = lambda root, config: overdue_silences(root, config, repair=False)
    if declared_devices not in plugins.DOCTOR_CHECKS: plugins.DOCTOR_CHECKS.append(declared_devices)
    plugins.DOCTOR_WITHOUT_REPAIRS[declared_devices] = declared_devices
