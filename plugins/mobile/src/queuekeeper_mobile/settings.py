"""The lane keys of the mobile plugins: the physical devices a lane's run uses and the minutes it may hold them."""
from queuekeeper import config, extensions


def device_keys(name, lane):
    """A lane's "devices", the entries of the device record its run uses, which a device pass's own --device options
    say without it. Only an admitted lane waits for admission, which they bound (lane_devices)."""
    if "devices" not in lane: return
    keys = lane["devices"]
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError(f"{name}.devices must be a non-empty list of device entries, such as \"iPhone\"")
    if config.unadmitted(lane):
        raise ValueError(f"{name}.devices needs an admitted lane: it bounds the wait for admission, and a self lane "
                         "is not admitted")


def lane_devices(lane):
    """The device-record entries a lane's run uses (vocaro-engineering#292): its "devices", for a project's script
    that calls device_pass.run, or each --device of an `qk device-pass` command, up to that command's own --, which
    the words of the command it runs follow. Any other lane uses none."""
    if lane.get("devices"): return list(lane["devices"])
    command = lane.get("command") or []
    if "device-pass" not in command: return []
    words = command[command.index("device-pass") + 1:]
    if "--" in words: words = words[:words.index("--")]
    return [word.split("=", 1)[1] if word.startswith("--device=") else words[index + 1]
            for index, word in enumerate(words) if word.startswith("--device=")
            or (word == "--device" and index + 1 < len(words))]


class DeviceKeys(extensions.LaneKey):
    def check(self, name, lane):
        device_keys(name, lane)


class DeviceMinutes(extensions.LaneKey):
    def check(self, name, lane):
        minutes = lane["deviceRunMinutes"]
        if type(minutes) is not int or minutes <= 0:
            raise ValueError(f"{name}.deviceRunMinutes must be a positive integer")
        if not lane_devices(lane) or config.unadmitted(lane):
            raise ValueError(f"{name}.deviceRunMinutes needs an admitted device lane")


def register():
    extensions.LANE_KEYS.update(devices=DeviceKeys(), deviceRunMinutes=DeviceMinutes())
